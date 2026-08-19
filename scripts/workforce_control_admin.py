#!/usr/bin/env python3
"""Guarded operator controls for the Workforce Control runtime.

This command is intentionally separate from agent tools.  It never enables a
write-capable mode without an explicit confirmation token and an immediate,
owner-only SQLite backup.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any

from hermes_cli import kanban_db
from plugins.workforce_control.store import (
    dashboard_snapshot,
    ensure_schema,
    schema_present,
    set_runtime_mode,
)


CONFIRMATIONS = {
    "init": "INITIALIZE-WORKFORCE-CONTROL",
    "shadow": "ENABLE-WORKFORCE-SHADOW",
    "apply": "ENABLE-WORKFORCE-APPLY",
    "paused": "PAUSE-WORKFORCE",
}


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _read_snapshot(database: Path) -> dict[str, Any]:
    if not database.is_file():
        return {"available": False, "runtime": None, "database": str(database)}
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        snapshot = dashboard_snapshot(conn)
    finally:
        conn.close()
    snapshot["database"] = str(database)
    return snapshot


def _backup(database: Path, output: Path) -> dict[str, Any]:
    database = database.resolve()
    output = output.resolve()
    if output == database:
        raise ValueError("backup output must differ from the live database")
    if output.exists():
        raise FileExistsError(f"refusing to overwrite backup: {output}")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output.parent, 0o700)
    source = sqlite3.connect(str(database))
    target = sqlite3.connect(str(output))
    try:
        source.backup(target)
        result = target.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError("backup integrity_check failed")
    finally:
        target.close()
        source.close()
    os.chmod(output, 0o600)
    return {"path": str(output), "sha256": _digest(output), "mode": "0600", "integrity": "ok"}


def _require_confirmation(value: str, expected: str) -> None:
    if value != expected:
        raise PermissionError(f"confirmation must be exactly {expected}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Read runtime state without creating tables")
    initialize = sub.add_parser("init", help="Initialize paused, kill-switched tables")
    initialize.add_argument("--backup-output", required=True, type=Path)
    initialize.add_argument("--confirm", required=True)
    mode = sub.add_parser("set-mode", help="Change runtime mode through a guarded operator action")
    mode.add_argument("--mode", choices=("paused", "shadow", "apply"), required=True)
    mode.add_argument("--reason", required=True)
    mode.add_argument("--confirm", required=True)
    mode.add_argument("--backup-output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    database = args.database.expanduser().resolve()
    if args.command == "status":
        print(json.dumps(_read_snapshot(database), indent=2, sort_keys=True))
        return 0
    if not database.is_file():
        raise FileNotFoundError(database)
    if args.command == "init":
        _require_confirmation(args.confirm, CONFIRMATIONS["init"])
        backup = _backup(database, args.backup_output)
        with kanban_db.connect_closing(database) as conn:
            ensure_schema(conn)
        result = _read_snapshot(database)
        result.update({"action": "initialized", "backup": backup})
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    _require_confirmation(args.confirm, CONFIRMATIONS[args.mode])
    backup = None
    if args.mode != "paused":
        if args.backup_output is None:
            raise ValueError("--backup-output is required before enabling shadow or apply mode")
        backup = _backup(database, args.backup_output)
    with kanban_db.connect_closing(database) as conn:
        if not schema_present(conn):
            raise RuntimeError("Workforce Control is not initialized")
        runtime = set_runtime_mode(
            conn,
            mode=args.mode,
            kill_switch=args.mode == "paused",
            reason=args.reason,
        )
    print(json.dumps({"action": "mode_changed", "runtime": runtime, "backup": backup}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
