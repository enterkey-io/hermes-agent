#!/usr/bin/env python3
"""Inventory host-cron delivery and failure paths without exposing messages."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any


ROUTE_PATTERNS = {
    "telegram": re.compile(r"telegram", re.I),
    "matrix": re.compile(r"matrix", re.I),
    "photon": re.compile(r"photon", re.I),
    "elliott-msg": re.compile(r"elliott-msg\.sh", re.I),
    "hermes-send": re.compile(r"\bhermes\s+send\b", re.I),
}
SCRIPT_PATH = re.compile(r"(?P<path>/home/[^\s'\"]+\.(?:py|sh))")


def _scheduled_lines(text: str) -> list[str]:
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip()
        and not line.lstrip().startswith("#")
        and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", line)
    ]


def _owner(line: str) -> tuple[str, str | None, str]:
    match = re.search(r"/profiles/([a-z0-9_-]+)", line)
    if match:
        profile = match.group(1)
    elif "xenia-" in line:
        profile = "xenia"
    elif "enterkey-poly" in line or "nanoclaw" in line:
        profile = "main"
    elif "elliott-calendar" in line:
        profile = "grace"
    else:
        profile = "main"
    room = {
        "xenia": "director-trading",
        "grace": "executive-support",
        "alina": "director-agent-systems",
        "aurora": "admin",
        "main": "director-operations",
    }.get(profile)
    privacy = "private-personal" if "wardrobe" in line.casefold() else "operational"
    return profile, room, privacy


def _markers(line: str) -> tuple[list[str], list[dict[str, Any]]]:
    command_markers = sorted(name for name, pattern in ROUTE_PATTERNS.items() if pattern.search(line))
    scripts = []
    for match in SCRIPT_PATH.finditer(line):
        path = Path(match.group("path"))
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8-sig", errors="replace")
        found = sorted(name for name, pattern in ROUTE_PATTERNS.items() if pattern.search(content))
        if found:
            scripts.append({"path": str(path), "markers": found})
    return command_markers, scripts


def build(text: str) -> dict[str, Any]:
    entries = []
    for ordinal, line in enumerate(_scheduled_lines(text), 1):
        profile, room, privacy = _owner(line)
        command_markers, script_markers = _markers(line)
        routes = sorted(set(command_markers).union(
            marker for item in script_markers for marker in item["markers"]
        ))
        basename = "unknown"
        paths = list(SCRIPT_PATH.finditer(line))
        if paths:
            basename = Path(paths[0].group("path")).name
        migration = privacy == "operational" and bool(routes)
        entries.append({
            "ordinal": ordinal,
            "line_sha256": hashlib.sha256(line.encode()).hexdigest(),
            "command_basename": basename,
            "owner_profile": profile,
            "normal_classification": "private-personal" if privacy == "private-personal" else "local-only",
            "quiet_success": True,
            "current_failure_route_markers": command_markers,
            "script_failure_route_markers": script_markers,
            "intended_failure_room": room if migration else None,
            "migration_required": migration,
            "staged_action_not_executed": (
                f"replace legacy/direct failure delivery with explicit buzz:<ROOM_UUID:{room}>"
                if migration else None
            ),
            "rollback_source": "owner-only pre-cutover crontab backup" if migration else None,
        })
    return {
        "schema_version": 1,
        "mutation_performed": False,
        "active_host_schedules": len(entries),
        "migration_required": sum(item["migration_required"] for item in entries),
        "entries": entries,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path)
    args = parser.parse_args(argv)
    if args.input:
        text = args.input.read_text(encoding="utf-8")
    else:
        proc = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=False,
        )
        text = proc.stdout if proc.returncode == 0 else ""
    print(json.dumps(build(text), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
