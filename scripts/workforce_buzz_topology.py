#!/usr/bin/env python3
"""Validate intended Buzz rooms and compare them with a live relay, read-only."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
from typing import Any

import yaml

from hermes_cli.workforce_org import load_organization


class BuzzTopologyError(ValueError):
    pass


def load_topology(path: Path, organization_path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    if raw.get("schema_version") != 1 or not isinstance(raw.get("rooms"), list):
        raise BuzzTopologyError("invalid Buzz topology schema")
    org = load_organization(organization_path)
    names: set[str] = set()
    rooms: list[dict[str, Any]] = []
    for raw_room in raw["rooms"]:
        if not isinstance(raw_room, dict):
            raise BuzzTopologyError("each Buzz room must be a mapping")
        name = str(raw_room.get("name") or "").strip()
        if not name or name.casefold() in names:
            raise BuzzTopologyError(f"missing or duplicate Buzz room: {name!r}")
        names.add(name.casefold())
        status = str(raw_room.get("status") or "")
        if status not in {"confirmed", "candidate"}:
            raise BuzzTopologyError(f"{name}: invalid room status")
        members = [str(value).casefold() for value in raw_room.get("members", [])]
        if not members or len(members) != len(set(members)):
            raise BuzzTopologyError(f"{name}: members must be unique and non-empty")
        for member in members:
            item = org.get(member)
            if member != "elliott" and (
                not item.operational or item.status not in {"active", "planned"}
            ):
                raise BuzzTopologyError(f"{name}: non-operational member {member}")
        if {"amy", "kourtnie"} & set(members):
            raise BuzzTopologyError(f"{name}: friend profiles cannot join operational rooms")
        rooms.append(
            {
                "name": name,
                "status": status,
                "visibility": str(raw_room.get("visibility") or "private"),
                "purpose": str(raw_room.get("purpose") or "").strip(),
                "members": members,
            }
        )
    admin = next((room for room in rooms if room["name"].casefold() == "admin"), None)
    if admin is None or set(admin["members"]) != {
        "elliott", "aurora", "chloe", "grace", "milena"
    }:
        raise BuzzTopologyError("admin membership must exactly match the confirmed roster")
    director_rooms = [room for room in rooms if room["name"].startswith("director-")]
    for room in director_rooms:
        if "chloe" not in room["members"] or "aurora" not in room["members"]:
            raise BuzzTopologyError(f"{room['name']}: Aurora and Chloe visibility required")
    raw_homes = raw.get("profile_home_rooms")
    if not isinstance(raw_homes, dict):
        raise BuzzTopologyError("profile_home_rooms must be a mapping")
    home_rooms = {str(agent).casefold(): str(room) for agent, room in raw_homes.items()}
    operational = {item.agent for item in org.operational_agents()}
    if set(home_rooms) != operational:
        missing = sorted(operational - set(home_rooms))
        extra = sorted(set(home_rooms) - operational)
        raise BuzzTopologyError(f"home-room roster mismatch; missing={missing}, extra={extra}")
    by_name = {room["name"].casefold(): room for room in rooms}
    for agent, room_name in home_rooms.items():
        room = by_name.get(room_name.casefold())
        if room is None:
            raise BuzzTopologyError(f"{agent}: unknown home room {room_name}")
        if agent not in room["members"]:
            raise BuzzTopologyError(f"{agent}: not a member of home room {room_name}")
    raw_additional = raw.get("profile_additional_rooms") or {}
    if not isinstance(raw_additional, dict):
        raise BuzzTopologyError("profile_additional_rooms must be a mapping")
    additional_rooms: dict[str, list[str]] = {}
    managed_memberships = {
        agent: {room["name"].casefold() for room in rooms if agent in room["members"]}
        for agent in operational
    }
    for raw_agent, raw_names in raw_additional.items():
        agent = str(raw_agent).casefold()
        if agent not in operational:
            raise BuzzTopologyError(f"additional-room profile is not operational: {agent}")
        if not isinstance(raw_names, list):
            raise BuzzTopologyError(f"{agent}: additional rooms must be a list")
        names_for_agent = [str(name).strip() for name in raw_names]
        folded = [name.casefold() for name in names_for_agent]
        if any(not name for name in names_for_agent) or len(folded) != len(set(folded)):
            raise BuzzTopologyError(f"{agent}: additional rooms must be unique and non-empty")
        duplicates = sorted(set(folded) & managed_memberships[agent])
        if duplicates:
            raise BuzzTopologyError(
                f"{agent}: additional rooms duplicate managed membership: {', '.join(duplicates)}"
            )
        additional_rooms[agent] = names_for_agent
    return {
        "schema_version": 1,
        "rooms": rooms,
        "profile_home_rooms": home_rooms,
        "profile_additional_rooms": additional_rooms,
        "member_aliases": {
            item.display_name.casefold(): item.agent
            for item in org.agents.values()
        },
    }


def _run_buzz(cli: str, relay: str, args: list[str]) -> Any:
    env = dict(os.environ)
    if not env.get("BUZZ_PRIVATE_KEY"):
        raise BuzzTopologyError("BUZZ_PRIVATE_KEY must be present in the process environment")
    proc = subprocess.run(
        [cli, "--relay", relay, *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    if proc.returncode:
        try:
            detail = json.loads(proc.stderr).get("message")
        except (json.JSONDecodeError, AttributeError):
            detail = f"buzz exited {proc.returncode}"
        raise BuzzTopologyError(str(detail))
    return json.loads(proc.stdout or "null")


def inspect_live(cli: str, relay: str) -> list[dict[str, Any]]:
    channels = _run_buzz(cli, relay, ["channels", "list"])
    if not isinstance(channels, list):
        raise BuzzTopologyError("Buzz channels list returned a non-list")
    user_names: dict[str, str] = {}
    live: list[dict[str, Any]] = []
    for channel in channels:
        channel_id = str(channel.get("channel_id") or "")
        members = _run_buzz(
            cli, relay, ["channels", "members", "--channel", channel_id]
        )
        names: list[str] = []
        for member in members if isinstance(members, list) else []:
            pubkey = str(member.get("pubkey") or "")
            if not pubkey:
                continue
            if pubkey not in user_names:
                profiles = _run_buzz(
                    cli, relay, ["users", "get", "--pubkey", pubkey]
                )
                profile = profiles[0] if isinstance(profiles, list) and profiles else {}
                label = str(
                    profile.get("display_name")
                    or profile.get("name")
                    or "unresolved-member"
                )
                user_names[pubkey] = label.casefold()
            names.append(user_names[pubkey])
        live.append(
            {
                "name": str(channel.get("name") or ""),
                "description": str(channel.get("description") or ""),
                "members": sorted(set(names)),
            }
        )
    return live


def compare(topology: dict[str, Any], live: list[dict[str, Any]]) -> dict[str, Any]:
    live_by_name = {room["name"].casefold(): room for room in live}
    intended_names = {room["name"].casefold() for room in topology["rooms"]}
    drift: list[dict[str, Any]] = []
    aliases = topology.get("member_aliases") or {}
    for intended in topology["rooms"]:
        actual = live_by_name.get(intended["name"].casefold())
        expected = set(intended["members"])
        observed = {
            aliases.get(str(member).casefold(), str(member).casefold())
            for member in (actual["members"] if actual else [])
        }
        drift.append(
            {
                "room": intended["name"],
                "status": intended["status"],
                "exists": actual is not None,
                "missing_members": sorted(expected - observed),
                "unexpected_members": sorted(observed - expected),
                "matches": actual is not None and expected == observed,
            }
        )
    unmanaged = sorted(
        room["name"] for room in live if room["name"].casefold() not in intended_names
    )
    return {
        "valid": True,
        "mutation_performed": False,
        "rooms": drift,
        "unmanaged_live_rooms": unmanaged,
        "matching_rooms": sum(1 for room in drift if room["matches"]),
        "intended_rooms": len(drift),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topology", type=Path, required=True)
    parser.add_argument("--organization", type=Path, required=True)
    parser.add_argument("--live-json", type=Path)
    parser.add_argument("--relay")
    parser.add_argument("--cli", default="buzz")
    args = parser.parse_args(argv)
    try:
        topology = load_topology(args.topology, args.organization)
        if args.live_json:
            live = json.loads(args.live_json.read_text(encoding="utf-8"))
        elif args.relay:
            live = inspect_live(args.cli, args.relay)
        else:
            live = []
        report = compare(topology, live)
    except (BuzzTopologyError, OSError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        report = {"valid": False, "mutation_performed": False, "error": str(exc)}
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
