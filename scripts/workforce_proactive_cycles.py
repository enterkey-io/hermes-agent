#!/usr/bin/env python3
"""Render and validate the proactive operating-cycle runbook for one host."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

import yaml

from hermes_cli.runbook_schema import split_frontmatter


ROOM_TOKEN = re.compile(r"<ROOM_UUID:([a-z0-9-]+)>")


def render(template: Path, room_map_path: Path) -> str:
    room_map = yaml.safe_load(room_map_path.read_text(encoding="utf-8-sig")) or {}
    if not isinstance(room_map, dict):
        raise ValueError("room map must be a mapping")
    text = template.read_text(encoding="utf-8")

    def replace(match: re.Match[str]) -> str:
        room = match.group(1)
        value = str(room_map.get(room) or "")
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", value):
            raise ValueError(f"missing or invalid room UUID for {room}")
        return value

    rendered = ROOM_TOKEN.sub(replace, text)
    parsed = split_frontmatter(rendered)
    schedules = parsed.metadata["schedules"]
    if len(schedules) != 10:
        raise ValueError("proactive cycle runbook must define exactly ten schedules")
    return rendered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--room-map", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    rendered = render(args.template, args.room_map)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        args.output.chmod(0o600)
    parsed = split_frontmatter(rendered)
    print(json.dumps({
        "valid": True,
        "id": parsed.metadata["id"],
        "slug": parsed.metadata["slug"],
        "schedules": len(parsed.metadata["schedules"]),
        "profiles": [item["profile"] for item in parsed.metadata["schedules"]],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
