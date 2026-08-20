#!/usr/bin/env python3
"""Apply the reviewed 2026-08-20 proactive-workforce Kanban audit.

The operation is intentionally narrow and fail closed:

* the audit file must have the operator-supplied SHA-256;
* its record ids must exactly match the live non-terminal task set;
* Workforce Control must already be paused and kill-switched;
* apply mode requires an immediate owner-only SQLite backup; and
* every disposition is preserved as a comment and task event.

This is an operator recovery tool, not an unattended reconciler.  It does not
delete tasks, runs, logs, attachments, or workspaces.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any


CONFIRM = "RECONCILE-PROACTIVE-WORKFORCE-BOARD"

# Broken/stale lanes with no executable current candidate.  The current Plaud
# repair is deliberately excluded because its failure was a repairable invalid
# skill binding, not a failed implementation attempt.
ARCHIVE_STALE = {
    "t_1abc9dca",
    "t_ac8159cd",
    "t_591a2745",
    "t_dbfdc33e",
    "t_59fb2cb6",
    "t_88a6f8e6",
    "t_9e453c45",
    "t_ed777d66",
}

# Children of the stale lanes above, or old signals without an executable
# outcome.  Archiving them prevents an archived parent from accidentally
# promoting a now-meaningless child.
ARCHIVE_INTERNAL = {
    "t_f240cc49",
    "t_fdf364be",
    "t_9a1959a2",
    "t_ebd37620",
    "t_90253681",
    "t_a7344eac",
    "t_a104950e",
    "t_200b2aa4",
    "t_f6ecef63",
    "t_6a5256a0",
}

READY = {
    "t_90820469",  # current Plaud implementation; remove invalid skill
    "t_810b3d10",  # VT destination-boundary P1, test-only
    "t_3c7756a1",  # Aurora review of the exact VT proposal
    "t_54dac687",  # Aurora's internal current-goal fit decision
}

RESERVED = {
    "t_7d09c9e8": "A Microsoft 365 tenant administrator must enable the exact Graph transcript permission, then Alina verifies metadata only.",
    "t_86bda071": "Activation requires approval evidence bound to the exact reviewed VT proposal; broad project authorization is not a substitute.",
    "t_15272d19": "The strategy owner must resolve the specific social positioning and engagement-policy questions before production work resumes.",
}

# parent -> child.  These are real implementation/QA/acceptance dependencies;
# they replace prose-only "waiting on" claims with enforceable graph edges.
DEPENDENCIES = (
    ("t_7d09c9e8", "t_997e59bc"),
    ("t_997e59bc", "t_b17e6d11"),
    ("t_15272d19", "t_a40c0482"),
    ("t_15272d19", "t_587619b7"),
    ("t_15272d19", "t_de57ef5a"),
    ("t_a40c0482", "t_471b6a8d"),
    ("t_587619b7", "t_471b6a8d"),
    ("t_de57ef5a", "t_471b6a8d"),
    ("t_3c7756a1", "t_86bda071"),
    ("t_810b3d10", "t_77418e14"),
    ("t_77418e14", "t_b31e3754"),
    ("t_90820469", "t_c50ee2ec"),
    ("t_c50ee2ec", "t_0d506e0b"),
)

INVALID_SKILL_REPAIRS = {"t_90820469", "t_c50ee2ec"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _backup(source: Path, output: Path) -> dict[str, str]:
    if output.exists():
        raise FileExistsError(f"refusing to overwrite backup: {output}")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output.parent, 0o700)
    src = sqlite3.connect(str(source))
    dst = sqlite3.connect(str(output))
    try:
        src.backup(dst)
        check = dst.execute("PRAGMA integrity_check").fetchone()
        if not check or check[0] != "ok":
            raise RuntimeError("backup integrity_check failed")
    finally:
        dst.close()
        src.close()
    os.chmod(output, 0o600)
    return {"path": str(output), "sha256": _sha256(output), "mode": "0600"}


def _event(conn: sqlite3.Connection, task_id: str, kind: str, payload: dict[str, Any]) -> None:
    now = int(time.time())
    conn.execute(
        "INSERT INTO task_events(task_id,kind,payload,created_at) VALUES(?,?,?,?)",
        (task_id, kind, json.dumps(payload, sort_keys=True), now),
    )


def _comment(conn: sqlite3.Connection, task_id: str, body: str) -> None:
    now = int(time.time())
    conn.execute(
        "INSERT INTO task_comments(task_id,author,body,created_at) VALUES(?,?,?,?)",
        (task_id, "root", body, now),
    )
    _event(conn, task_id, "commented", {"author": "root", "len": len(body)})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--audit", required=True, type=Path)
    parser.add_argument("--expected-audit-sha256", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm")
    parser.add_argument("--backup-output", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    database = args.database.expanduser().resolve()
    audit_path = args.audit.expanduser().resolve()
    actual_audit_hash = _sha256(audit_path)
    if actual_audit_hash != args.expected_audit_sha256:
        raise RuntimeError("audit SHA-256 mismatch")
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    records = {str(row["id"]): row for row in audit["records"]}

    conn = sqlite3.connect(str(database))
    conn.row_factory = sqlite3.Row
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"database integrity check failed: {integrity}")
        live_open = {
            row["id"]
            for row in conn.execute(
                "SELECT id FROM tasks WHERE status NOT IN ('done','archived')"
            )
        }
        if live_open != set(records):
            raise RuntimeError(
                "live non-terminal task set drifted from audit: "
                f"added={sorted(live_open - set(records))}, "
                f"missing={sorted(set(records) - live_open)}"
            )
        runtime = conn.execute(
            "SELECT mode,kill_switch FROM wc_runtime WHERE singleton=1"
        ).fetchone()
        if runtime is None or runtime["mode"] != "paused" or int(runtime["kill_switch"]) != 1:
            raise RuntimeError("Workforce Control must be paused and kill-switched")

        archive = {
            task_id
            for task_id, row in records.items()
            if row["classification"] in {
                "duplicate/superseded",
                "speculative_or_not_execution_ready",
            }
        } | ARCHIVE_STALE | ARCHIVE_INTERNAL
        complete = {
            task_id
            for task_id, row in records.items()
            if row["classification"] == "already_complete"
        }
        dependency_children = {child for _, child in DEPENDENCIES}
        retained = set(records) - archive - complete
        expected_retained = set(READY) | set(RESERVED) | dependency_children
        if retained != expected_retained:
            raise RuntimeError(
                "disposition ledger is incomplete: "
                f"unexpected={sorted(retained - expected_retained)}, "
                f"missing={sorted(expected_retained - retained)}"
            )

        plan = {
            "audit_sha256": actual_audit_hash,
            "archive": len(archive),
            "complete": len(complete),
            "ready": sorted(READY),
            "reserved": sorted(RESERVED),
            "dependency_edges": len(DEPENDENCIES),
            "retained_open": len(retained),
            "invalid_skill_repairs": sorted(INVALID_SKILL_REPAIRS),
        }
        if not args.apply:
            print(json.dumps({"mode": "dry_run", **plan}, indent=2, sort_keys=True))
            return 0
        if args.confirm != CONFIRM:
            raise PermissionError(f"confirmation must be exactly {CONFIRM}")
        if args.backup_output is None:
            raise ValueError("--backup-output is required in apply mode")
        backup = _backup(database, args.backup_output.expanduser().resolve())

        now = int(time.time())
        with conn:
            for task_id in sorted(archive):
                row = records[task_id]
                _comment(
                    conn,
                    task_id,
                    "2026-08-20 audited reconciliation: "
                    f"{row['classification']}. {row['evidence']} "
                    f"Disposition: {row['next_action']}",
                )
                conn.execute(
                    "UPDATE tasks SET status='archived',claim_lock=NULL,"
                    "claim_expires=NULL,worker_pid=NULL,current_run_id=NULL "
                    "WHERE id=?",
                    (task_id,),
                )
                conn.execute(
                    "UPDATE wc_items SET current_state='archived',updated_at=? "
                    "WHERE task_id=? AND current_state='open'",
                    (now, task_id),
                )
                _event(
                    conn,
                    task_id,
                    "proactive_board_reconciled_archived",
                    {"classification": row["classification"], "audit_sha256": actual_audit_hash},
                )

            for task_id in sorted(complete):
                row = records[task_id]
                summary = (
                    "Closed from the reviewed 2026-08-20 whole-board audit. "
                    f"{row['evidence']}"
                )
                _comment(conn, task_id, summary)
                conn.execute(
                    "UPDATE tasks SET status='done',completed_at=?,claim_lock=NULL,"
                    "claim_expires=NULL,worker_pid=NULL,current_run_id=NULL,"
                    "block_kind=NULL,consecutive_failures=0,last_failure_error=NULL "
                    "WHERE id=?",
                    (now, task_id),
                )
                conn.execute(
                    "UPDATE wc_items SET current_state='complete',updated_at=? "
                    "WHERE task_id=? AND current_state='open'",
                    (now, task_id),
                )
                _event(
                    conn,
                    task_id,
                    "proactive_board_reconciled_complete",
                    {"audit_sha256": actual_audit_hash, "evidence": row["evidence"]},
                )

            # Remove old links among the retained set before installing the
            # reviewed graph. Links to archived history remain for provenance.
            placeholders = ",".join("?" for _ in retained)
            conn.execute(
                f"DELETE FROM task_links WHERE child_id IN ({placeholders}) "
                f"AND parent_id IN ({placeholders})",
                tuple(sorted(retained)) * 2,
            )
            for parent, child in DEPENDENCIES:
                conn.execute(
                    "INSERT OR IGNORE INTO task_links(parent_id,child_id) VALUES(?,?)",
                    (parent, child),
                )
                _event(conn, child, "linked", {"parent": parent, "child": child, "audit": actual_audit_hash})

            for task_id in INVALID_SKILL_REPAIRS:
                conn.execute("UPDATE tasks SET skills='[]' WHERE id=?", (task_id,))
                _event(
                    conn,
                    task_id,
                    "invalid_skill_binding_removed",
                    {"removed": "kanban-worker", "audit_sha256": actual_audit_hash},
                )

            for task_id in READY:
                conn.execute(
                    "UPDATE tasks SET status='ready',block_kind=NULL,claim_lock=NULL,"
                    "claim_expires=NULL,worker_pid=NULL,current_run_id=NULL,"
                    "consecutive_failures=0,last_failure_error=NULL WHERE id=?",
                    (task_id,),
                )
                _comment(
                    conn,
                    task_id,
                    "Ready after audited reconciliation. Execute only the bounded scope and acceptance tests already recorded on this card.",
                )
                _event(conn, task_id, "proactive_board_reconciled_ready", {"audit_sha256": actual_audit_hash})

            for task_id, reason in RESERVED.items():
                conn.execute(
                    "UPDATE tasks SET status='blocked',block_kind='needs_input',"
                    "claim_lock=NULL,claim_expires=NULL,worker_pid=NULL,current_run_id=NULL "
                    "WHERE id=?",
                    (task_id,),
                )
                _comment(
                    conn,
                    task_id,
                    "Retained-authority gate (not an internal handoff): " + reason,
                )
                _event(conn, task_id, "reserved_gate_reconciled", {"reason": reason, "audit_sha256": actual_audit_hash})

            # The X-research rescope decision belongs to Aurora, not Elliott.
            conn.execute(
                "UPDATE tasks SET assignee='aurora',consecutive_failures=0,last_failure_error=NULL "
                "WHERE id='t_54dac687'"
            )
            _event(conn, "t_54dac687", "assigned", {"assignee": "aurora", "reason": "internal goal-fit decision"})

            # A retained-authority gate may also have a prerequisite edge.
            # Keep its blocked/needs_input state; only ordinary children land
            # in the internal dependency queue.
            for task_id in dependency_children - set(RESERVED):
                conn.execute(
                    "UPDATE tasks SET status='todo',block_kind='dependency',"
                    "claim_lock=NULL,claim_expires=NULL,worker_pid=NULL,current_run_id=NULL,"
                    "consecutive_failures=0,last_failure_error=NULL WHERE id=?",
                    (task_id,),
                )
                _event(
                    conn,
                    task_id,
                    "dependency_wait",
                    {"reason": "reviewed task graph dependency", "kind": "dependency", "audit_sha256": actual_audit_hash},
                )

            # Raw Kanban lifecycle streaming to Elliott is explicitly retired.
            # Outcome delivery is handled by the responsible agent/workflow.
            conn.execute("DELETE FROM kanban_notify_subs")

        check = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise RuntimeError(f"post-apply integrity check failed: {check}")
        status_counts = {
            row["status"]: row["count"]
            for row in conn.execute("SELECT status,COUNT(*) count FROM tasks GROUP BY status")
        }
        open_human = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM tasks WHERE status='blocked' AND block_kind='needs_input' ORDER BY id"
            )
        ]
        print(
            json.dumps(
                {
                    "mode": "applied",
                    **plan,
                    "backup": backup,
                    "integrity": check,
                    "status_counts": status_counts,
                    "needs_input": open_human,
                    "notify_subscriptions": conn.execute(
                        "SELECT COUNT(*) FROM kanban_notify_subs"
                    ).fetchone()[0],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
