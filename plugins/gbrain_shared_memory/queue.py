"""Durable, idempotent turn queue for shared GBrain capture."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

MAX_MESSAGE_CHARS = 8000


def _queue_dir(home: Path) -> Path:
    path = home / "state" / "gbrain-memory" / "outbox"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def _idempotency_key(
    profile: str,
    session_id: str,
    turn_id: str,
    user_message: str,
) -> str:
    normalized = " ".join(user_message.split())
    message_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    identity = "\0".join((profile, session_id, turn_id, message_hash))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def enqueue_turn(
    *,
    home: Path,
    profile: str,
    session_id: str,
    turn_id: str,
    user_message: str,
    platform: str,
) -> Path:
    """Atomically enqueue one bounded owner turn. Duplicate input is a no-op."""
    bounded = user_message[:MAX_MESSAGE_CHARS]
    key = _idempotency_key(profile, session_id, turn_id, bounded)
    outbox = _queue_dir(home)
    destination = outbox / f"{key}.json"
    if destination.exists():
        return destination

    payload = {
        "schema_version": 1,
        "idempotency_key": key,
        "profile": profile,
        "session_id": session_id,
        "turn_id": turn_id,
        "platform": platform,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "user_message": bounded,
    }
    temporary = outbox / f".{key}.{os.getpid()}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
