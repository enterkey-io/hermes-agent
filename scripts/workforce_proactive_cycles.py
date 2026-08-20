#!/usr/bin/env python3
"""Render and validate the proactive operating-cycle runbook for one host."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from uuid import UUID

import yaml

from cron.jobs import parse_schedule
from hermes_cli.runbook_schema import split_frontmatter


ROOM_TOKEN = re.compile(r"<ROOM_UUID:([a-z0-9-]+)>")
BOUNDED_TOOLSETS = ["kanban", "workforce", "runbook", "no_mcp"]
BOUNDED_MAX_ITERATIONS = 8
BOUNDED_TOOL_BUDGET = {
    "max_calls": 6,
    "max_writes": 1,
    "max_detail_reads": 2,
    "max_list_items": 12,
    "allowed_tools": [
        "kanban_list", "kanban_show", "kanban_complete", "kanban_block",
        "kanban_request_review", "kanban_request_changes", "kanban_comment",
        "kanban_archive_stale", "kanban_attachments", "workforce_signal",
        "workforce_handoff", "runbook_list",
        "runbook_search", "runbook_get", "runbook_validate", "runbook_runs",
        "workforce_goals", "workforce_vision", "workforce_observe_buzz",
    ],
}


def render_room_tokens(template: Path, room_map_path: Path) -> str:
    room_map = yaml.safe_load(room_map_path.read_text(encoding="utf-8-sig")) or {}
    if not isinstance(room_map, dict):
        raise ValueError("room map must be a mapping")
    text = template.read_text(encoding="utf-8")

    def replace(match: re.Match[str]) -> str:
        room = match.group(1)
        value = str(room_map.get(room) or "")
        try:
            parsed_uuid = UUID(value)
        except (ValueError, AttributeError):
            raise ValueError(f"missing or invalid room UUID for {room}")
        canonical = str(parsed_uuid)
        if value.casefold() != canonical:
            raise ValueError(f"missing or invalid room UUID for {room}")
        return canonical

    rendered = ROOM_TOKEN.sub(replace, text)
    if ROOM_TOKEN.search(rendered):
        raise ValueError("unresolved room UUID token")
    return rendered


def render(template: Path, room_map_path: Path) -> str:
    rendered = render_room_tokens(template, room_map_path)
    parsed = split_frontmatter(rendered)
    schedules = parsed.metadata["schedules"]
    runtime = parsed.metadata["runtime"]
    if runtime.get("max_iterations") != BOUNDED_MAX_ITERATIONS:
        raise ValueError("proactive cycle runtime must enforce max_iterations=8")
    if runtime.get("tool_budget") != BOUNDED_TOOL_BUDGET:
        raise ValueError("proactive cycle runtime must use the bounded tool budget")
    steps = {
        str(step["step_key"]): step
        for step in parsed.metadata["steps"]
        if isinstance(step, dict) and step.get("step_key")
    }
    if not schedules:
        raise ValueError("proactive cycle runbook must define at least one schedule")
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    seen_step_keys: set[str] = set()
    for schedule in schedules:
        schedule_id = str(schedule.get("id") or "").strip()
        name = str(schedule.get("name") or "").strip()
        profile = str(schedule.get("profile") or "").strip()
        step_key = str(schedule.get("step_key") or "").strip()
        if not schedule_id or schedule_id in seen_ids:
            raise ValueError(f"missing or duplicate schedule id: {schedule_id!r}")
        if not name or name in seen_names:
            raise ValueError(f"missing or duplicate schedule name: {name!r}")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", profile):
            raise ValueError(f"invalid schedule profile: {profile!r}")
        if step_key not in steps or step_key in seen_step_keys:
            raise ValueError(f"missing or duplicate schedule step: {step_key!r}")
        executor = str(steps[step_key].get("executor_profile") or "").strip()
        if executor and executor != profile:
            raise ValueError(
                f"schedule {schedule_id!r} profile does not match step executor"
            )
        parse_schedule(str(schedule.get("schedule") or ""))
        destination = str(schedule.get("deliver") or "")
        if destination != "local":
            raise ValueError(
                f"schedule {schedule_id!r} must remain local-only; internal "
                "reconciliation may not deliver to an Elliott-visible room"
            )
        if schedule.get("enabled_toolsets") != BOUNDED_TOOLSETS:
            raise ValueError(
                f"schedule {schedule_id!r} must use the bounded proactive toolsets"
            )
        seen_ids.add(schedule_id)
        seen_names.add(name)
        seen_step_keys.add(step_key)
    return rendered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--room-map", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--rooms-only", action="store_true",
        help="render and validate room tokens without proactive-cycle-specific checks",
    )
    args = parser.parse_args(argv)
    rendered = (
        render_room_tokens(args.template, args.room_map)
        if args.rooms_only
        else render(args.template, args.room_map)
    )
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
        "rooms_only": bool(args.rooms_only),
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
