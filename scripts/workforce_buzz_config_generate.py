#!/usr/bin/env python3
"""Generate exact, non-executing Hermes Buzz profile configuration commands."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
import uuid

import yaml

from hermes_cli.workforce_org import load_organization
from scripts.workforce_buzz_topology import load_topology


def _nested(raw: dict[str, Any], dotted: str) -> Any:
    value: Any = raw
    for part in dotted.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def load_room_map(path: Path, topology: dict[str, Any]) -> dict[str, str]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    if not isinstance(raw, dict):
        raise ValueError("room map must be a mapping")
    result = {str(name): str(value) for name, value in raw.items()}
    expected = {room["name"] for room in topology["rooms"]}
    expected.update(
        name
        for names in topology.get("profile_additional_rooms", {}).values()
        for name in names
    )
    missing = sorted(expected - result.keys())
    if missing:
        raise ValueError(f"room map missing: {', '.join(missing)}")
    for name in expected:
        try:
            uuid.UUID(result[name])
        except ValueError as exc:
            raise ValueError(f"{name}: room id is not a UUID") from exc
    if len({result[name] for name in expected}) != len(expected):
        raise ValueError("room UUIDs must be unique")
    return result


def generate(
    *,
    organization_path: Path,
    topology_path: Path,
    room_map_path: Path,
    profiles_root: Path,
) -> dict[str, Any]:
    org = load_organization(organization_path)
    topology = load_topology(topology_path, organization_path)
    room_map = load_room_map(room_map_path, topology)
    rooms_by_agent: dict[str, list[str]] = {item.agent: [] for item in org.operational_agents()}
    for room in topology["rooms"]:
        for member in room["members"]:
            if member in rooms_by_agent:
                rooms_by_agent[member].append(room["name"])
    for agent, room_names in topology.get("profile_additional_rooms", {}).items():
        rooms_by_agent[agent].extend(room_names)
    changes = []
    for agent in org.operational_agents():
        profile_name = Path(agent.profile_path or agent.agent).name
        config_path = profiles_root / profile_name / "config.yaml"
        current: dict[str, Any] = {}
        if config_path.is_file():
            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8-sig")) or {}
            current = loaded if isinstance(loaded, dict) else {}
        names = sorted(rooms_by_agent[agent.agent], key=str.casefold)
        channels = ",".join(room_map[name] for name in names)
        home_name = topology["profile_home_rooms"][agent.agent]
        home = room_map[home_name]
        env = {"HERMES_HOME": str(profiles_root / profile_name)}
        values = {
            "gateway.platforms.buzz.extra.channels": channels,
            "gateway.platforms.buzz.extra.home_channel": home,
            "gateway.platforms.buzz.extra.require_mention": "true",
        }
        inverse_values = {
            key: _nested(current, key) for key in values
        }
        commands = [
            ["hermes", "config", "set", key, value]
            for key, value in values.items()
        ]
        inverse = []
        for key, value in inverse_values.items():
            if value is None:
                inverse.append(["hermes", "config", "unset", key])
            elif isinstance(value, bool):
                inverse.append(["hermes", "config", "set", key, str(value).lower()])
            else:
                inverse.append(["hermes", "config", "set", key, str(value)])
        changes.append(
            {
                "agent": agent.agent,
                "profile": profile_name,
                "status": agent.status,
                "rooms": names,
                "home_room": home_name,
                "environment": env,
                "commands_not_executed": commands,
                "inverse_commands_not_executed": inverse,
            }
        )
    return {
        "valid": True,
        "mutation_performed": False,
        "room_map_source": str(room_map_path),
        "profiles": changes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--organization", type=Path, required=True)
    parser.add_argument("--topology", type=Path, required=True)
    parser.add_argument("--room-map", type=Path, required=True)
    parser.add_argument("--profiles-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = generate(
            organization_path=args.organization,
            topology_path=args.topology,
            room_map_path=args.room_map,
            profiles_root=args.profiles_root,
        )
    except (OSError, ValueError, yaml.YAMLError) as exc:
        report = {"valid": False, "mutation_performed": False, "error": str(exc)}
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        args.output.chmod(0o600)
    print(rendered, end="")
    return 0 if report.get("valid") else 1


if __name__ == "__main__":
    raise SystemExit(main())
