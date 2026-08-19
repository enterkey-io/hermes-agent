#!/usr/bin/env python3
"""Classify every open card in a read-only copied Kanban database."""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Any


OPEN_STATUSES = {"triage", "todo", "scheduled", "ready", "running", "blocked", "review"}


def _normalized(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _read_only(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def reconcile(db_path: Path) -> dict[str, Any]:
    now = int(time.time())
    with _read_only(db_path) as conn:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        rows = conn.execute(
            "SELECT id,title,status,assignee,created_at,workspace_kind,workspace_path,"
            "claim_expires,current_run_id,consecutive_failures,last_failure_error,"
            "idempotency_key,block_kind,block_recurrences FROM tasks "
            "WHERE status NOT IN ('done','archived') ORDER BY created_at,id"
        ).fetchall()
        event_rows = conn.execute(
            "SELECT e.task_id,e.id,e.kind,e.created_at FROM task_events e "
            "JOIN (SELECT task_id,MAX(id) id FROM task_events GROUP BY task_id) x "
            "ON x.task_id=e.task_id AND x.id=e.id"
        ).fetchall()
    latest = {str(row["task_id"]): dict(row) for row in event_rows}
    title_groups: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        title_groups[(_normalized(str(row["title"])), str(row["assignee"] or ""))].append(row)

    dispositions: list[dict[str, Any]] = []
    for row in rows:
        task_id = str(row["id"])
        event = latest.get(task_id)
        evidence = [f"kanban:task/{task_id}"]
        if event:
            evidence.append(f"kanban:event/{event['id']}:{event['kind']}")
        classification = "still_required"
        reason = "Open work has no machine-checkable contrary evidence"
        action = "keep_current_state_pending_owner_review"
        confidence = "medium"
        risk = "low"
        required_authority = str(row["assignee"] or "aurora")
        target_id = None

        group = title_groups[(_normalized(str(row["title"])), str(row["assignee"] or ""))]
        if len(group) > 1 and task_id != str(group[0]["id"]):
            classification = "duplicate"
            reason = "Same normalized title and assignee as an older open card"
            action = "review_semantic_identity_then_link_and_archive"
            confidence = "medium"
            risk = "medium"
            required_authority = "aurora"
            target_id = str(group[0]["id"])
        elif re.search(r"\b(superseded|obsolete|replaced by)\b", str(row["title"]), re.I):
            classification = "superseded"
            reason = "Title explicitly marks the work as superseded or obsolete"
            action = "identify_replacement_then_link_and_archive"
            confidence = "medium"
            risk = "medium"
            required_authority = "aurora"
        elif event and event["kind"] in {"changes_requested", "review_reopened"}:
            classification = "failed_verification"
            reason = "Latest durable event records a failed review transition"
            action = "identify_outcome_then_create_one_remediation"
            confidence = "high"
            risk = "medium"
            required_authority = "aurora"
        elif row["status"] == "blocked" and row["block_kind"] in {"needs_input", "capability"}:
            classification = "external_blocker"
            reason = f"Typed {row['block_kind']} blocker must not be auto-unblocked"
            action = "retain_block_and_name_decision_condition"
            confidence = "high"
            risk = "high" if row["block_kind"] == "capability" else "medium"
            required_authority = "aurora_or_elliott"
        elif (
            row["status"] == "running"
            and (row["current_run_id"] is None or (row["claim_expires"] or 0) < now)
        ):
            classification = "broken_record"
            reason = "Running card has no current run or has an expired claim"
            action = "quarantine_for_run_repair"
            confidence = "high"
            risk = "medium"
            required_authority = "root"
        elif int(row["consecutive_failures"] or 0) >= 3:
            classification = "broken_record"
            reason = "Failure circuit has reached at least three consecutive failures"
            action = "quarantine_for_failure_repair"
            confidence = "high"
            risk = "medium"
            required_authority = "root"
        elif (
            row["status"] == "running"
            and row["workspace_kind"] == "worktree"
            and row["workspace_path"]
            and not Path(str(row["workspace_path"])).is_dir()
        ):
            classification = "broken_record"
            reason = "Active worktree card references a missing workspace"
            action = "quarantine_for_workspace_repair"
            confidence = "high"
            risk = "medium"
            required_authority = "root"
        elif row["status"] == "blocked":
            classification = "broken_record"
            reason = "Blocked card lacks a typed external-blocker classification"
            action = "review_and_type_blocker_without_auto_unblock"
            confidence = "medium"
            risk = "medium"
            required_authority = "aurora"

        dispositions.append({
            "task_id": task_id,
            "title_sha256": _digest(str(row["title"])),
            "title_length": len(str(row["title"])),
            "status": row["status"],
            "assignee": row["assignee"],
            "classification": classification,
            "confidence": confidence,
            "risk_tier": risk,
            "reason": reason,
            "evidence_references": evidence,
            "target_task_id": target_id,
            "proposed_action": action,
            "required_authority": required_authority,
            "mutation_performed": False,
        })

    counts: dict[str, int] = defaultdict(int)
    for item in dispositions:
        counts[item["classification"]] += 1
    return {
        "schema_version": 1,
        "source": str(db_path),
        "source_sha256": hashlib.sha256(db_path.read_bytes()).hexdigest(),
        "integrity_check": integrity,
        "generated_at": now,
        "mutation_performed": False,
        "open_cards": len(dispositions),
        "documented_prior_count": 116,
        "count_drift": len(dispositions) - 116,
        "classification_counts": dict(sorted(counts.items())),
        "dispositions": dispositions,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = reconcile(args.database)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.output.chmod(0o600)
    print(json.dumps({key: report[key] for key in ("open_cards", "count_drift", "classification_counts", "mutation_performed")}, indent=2))
    return 0 if report["integrity_check"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
