"""Durable at-most-once inbound ledger for BlueBubbles webhooks."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

from hermes_constants import get_hermes_home

_LOCK = threading.Lock()
_RETENTION_SECONDS = 30 * 24 * 60 * 60
_MAX_ROWS = 10_000


def inbound_idempotency_key(server_identity: str, message_guid: str) -> str:
    """Hash exactly the stable server identity and BlueBubbles message GUID."""
    payload = json.dumps(
        [str(server_identity), str(message_guid)],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ClaimResult:
    key: str
    action: str


class BlueBubblesInboundLedger:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else get_hermes_home() / "state.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK, self._connection() as conn:
            self._initialize(conn)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _initialize(conn: sqlite3.Connection) -> None:
        from hermes_state import apply_wal_with_fallback

        apply_wal_with_fallback(
            conn,
            db_label="state.db (bluebubbles_inbound_ledger)",
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS bluebubbles_inbound_ledger (
                idempotency_key TEXT PRIMARY KEY,
                server_identity TEXT NOT NULL,
                message_guid TEXT NOT NULL,
                state TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                event_type TEXT,
                stable_chat_id TEXT,
                raw_chat_guid TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                dispatched_at REAL,
                last_error TEXT,
                UNIQUE(server_identity, message_guid)
            )"""
        )
        conn.commit()

    def claim_or_merge(
        self,
        *,
        server_identity: str,
        message_guid: str,
        payload: dict[str, Any],
        event_type: str,
        stable_chat_id: Optional[str],
        raw_chat_guid: Optional[str],
    ) -> ClaimResult:
        key = inbound_idempotency_key(server_identity, message_guid)
        now = time.time()
        encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        with _LOCK:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    """SELECT state, payload_json, stable_chat_id,
                              raw_chat_guid
                       FROM bluebubbles_inbound_ledger
                       WHERE idempotency_key=?""",
                    (key,),
                ).fetchone()
                if row is None:
                    if not stable_chat_id:
                        conn.rollback()
                        return ClaimResult(key=key, action="invalid")
                    conn.execute(
                        """INSERT INTO bluebubbles_inbound_ledger
                           (idempotency_key, server_identity, message_guid,
                            state, payload_json, event_type, stable_chat_id,
                            raw_chat_guid, created_at, updated_at)
                           VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)""",
                        (
                            key,
                            server_identity,
                            message_guid,
                            encoded,
                            event_type,
                            stable_chat_id,
                            raw_chat_guid,
                            now,
                            now,
                        ),
                    )
                    action = "claimed"
                elif row["state"] == "pending":
                    previous = json.loads(row["payload_json"])
                    encoded = json.dumps(
                        _deep_merge(previous, payload),
                        ensure_ascii=True,
                        separators=(",", ":"),
                    )
                    conn.execute(
                        """UPDATE bluebubbles_inbound_ledger
                           SET payload_json=?, event_type=?, stable_chat_id=?,
                               raw_chat_guid=?, updated_at=?
                           WHERE idempotency_key=? AND state='pending'""",
                        (
                            encoded,
                            event_type,
                            stable_chat_id or row["stable_chat_id"],
                            raw_chat_guid or row["raw_chat_guid"],
                            now,
                            key,
                        ),
                    )
                    action = "merged"
                else:
                    action = "duplicate"
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        self._prune(now)
        return ClaimResult(key=key, action=action)

    def claim_for_dispatch(self, key: str) -> Optional[dict[str, Any]]:
        now = time.time()
        with _LOCK:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    """SELECT payload_json, message_guid
                       FROM bluebubbles_inbound_ledger
                       WHERE idempotency_key=? AND state='pending'""",
                    (key,),
                ).fetchone()
                if row is None:
                    conn.rollback()
                    return None
                updated = conn.execute(
                    """UPDATE bluebubbles_inbound_ledger
                       SET state='dispatching', dispatched_at=?, updated_at=?
                       WHERE idempotency_key=? AND state='pending'""",
                    (now, now, key),
                )
                if updated.rowcount != 1:
                    conn.rollback()
                    return None
                conn.commit()
                return {
                    "payload": json.loads(row["payload_json"]),
                    "message_guid": row["message_guid"],
                }
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def mark_completed(self, key: str) -> None:
        self._mark_terminal(key, "completed")

    def mark_failed(self, key: str, error: str) -> None:
        self._mark_terminal(key, "failed", error)

    def _mark_terminal(self, key: str, state: str, error: str = "") -> None:
        with _LOCK, self._connection() as conn:
            conn.execute(
                """UPDATE bluebubbles_inbound_ledger
                   SET state=?, updated_at=?, last_error=?
                   WHERE idempotency_key=? AND state='dispatching'""",
                (state, time.time(), error[:500] or None, key),
            )
            conn.commit()

    def _prune(self, now: float) -> None:
        try:
            with _LOCK, self._connection() as conn:
                conn.execute(
                    """DELETE FROM bluebubbles_inbound_ledger
                       WHERE state IN ('completed', 'failed')
                         AND updated_at < ?""",
                    (now - _RETENTION_SECONDS,),
                )
                total = conn.execute(
                    "SELECT COUNT(*) FROM bluebubbles_inbound_ledger"
                ).fetchone()[0]
                excess = max(0, total - _MAX_ROWS)
                if excess:
                    conn.execute(
                        """DELETE FROM bluebubbles_inbound_ledger
                           WHERE idempotency_key IN (
                               SELECT idempotency_key
                               FROM bluebubbles_inbound_ledger
                               WHERE state IN ('completed', 'failed')
                               ORDER BY updated_at ASC
                               LIMIT ?
                           )""",
                        (excess,),
                    )
                conn.commit()
        except Exception:
            pass


def _deep_merge(existing: Any, update: Any) -> Any:
    if not isinstance(existing, dict) or not isinstance(update, dict):
        return update
    merged = dict(existing)
    for key, value in update.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged
