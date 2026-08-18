#!/usr/bin/env python3
"""Prepare or apply deterministic Cron-to-Buzz delivery changes.

Dry-run is the default. Applying requires a verified workforce backup and a
complete room-name-to-UUID map. The tool never changes schedules or jobs owned
by non-operational/friend profiles.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

import yaml

from scripts.workforce_delivery_inventory import build_manifest, _hidden_routes


PLATFORM_REPLACEMENTS = (
    (re.compile(r"telegram", re.I), "Buzz"),
    (re.compile(r"matrix", re.I), "Buzz"),
    (re.compile(r"photon", re.I), "Buzz"),
)
MANAGED_DELIVERY_BLOCK = re.compile(
    r"\n*\[WORKFORCE DELIVERY POLICY\]\n.*\Z",
    re.DOTALL,
)


def _load_room_map(path: Path) -> dict[str, str]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    if not isinstance(raw, dict):
        raise ValueError("room map must be a mapping of room names to UUIDs")
    result = {str(key): str(value) for key, value in raw.items()}
    for name, value in result.items():
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", value):
            raise ValueError(f"{name}: room id is not a UUID")
    return result


def _verified_backup(path: Path) -> None:
    result_path = path / "restore-test.json"
    archive = path / "workforce-profiles.tar"
    checksums = path / "SHA256SUMS"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if result.get("valid") is not True or result.get("mismatches"):
        raise ValueError("backup restoration test is not valid")
    if not archive.is_file() or not checksums.is_file():
        raise ValueError("verified backup is incomplete")


def _rewrite_prompt(value: str, room: str, *, quiet_success: bool) -> str:
    rewritten = value
    for pattern, replacement in PLATFORM_REPLACEMENTS:
        rewritten = pattern.sub(replacement, rewritten)
    rewritten = MANAGED_DELIVERY_BLOCK.sub("", rewritten).rstrip()
    success_policy = (
        "Routine success is silent when there is no decision-changing result, "
        "exception, blocker, or runbook-required report."
        if quiet_success else
        "Deliver the runbook-required report on its normal cadence; do not suppress "
        "it merely because it reports no exception or change."
    )
    marker = (
        "\n\n[WORKFORCE DELIVERY POLICY]\n"
        f"Return team-facing output only through the Cron destination for Buzz room `{room}`. "
        "Do not call a platform messaging tool or use a fallback destination. "
        f"{success_policy}\n"
    )
    return rewritten + marker


def _atomic_write_json(path: Path, payload: Any) -> None:
    mode = path.stat().st_mode & 0o777
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def migrate(
    *,
    profiles_root: Path,
    organization: Path,
    policy: Path,
    topology: Path,
    room_map: dict[str, str],
    apply: bool,
) -> dict[str, Any]:
    manifest = build_manifest(profiles_root, organization, policy, topology)
    if not manifest["valid"]:
        raise ValueError("delivery manifest is not valid")
    if apply and manifest["summary"].get("registry_cron_mismatches"):
        raise ValueError(
            "delivery cutover is blocked until every enabled Cron job has a "
            "reviewed Workflow Registry/runbook link"
        )
    by_profile: dict[str, list[dict[str, Any]]] = {}
    for item in manifest["jobs"]:
        if item["classification"] in {"team", "local-only"}:
            by_profile.setdefault(item["profile"], []).append(item)
    changes: list[dict[str, Any]] = []
    for profile, policies in sorted(by_profile.items()):
        path = profiles_root / profile / "cron" / "jobs.json"
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        rows = payload.get("jobs", []) if isinstance(payload, dict) else payload
        jobs_by_id = {str(job.get("id")): job for job in rows if isinstance(job, dict)}
        profile_changed = False
        for item in policies:
            job = jobs_by_id.get(item["job_id"])
            if job is None:
                raise ValueError(f"job disappeared: {profile}/{item['job_id']}")
            script_routes = [
                route for route in _hidden_routes(job, profiles_root / profile)
                if route["source"] == "script"
            ]
            if script_routes and item["classification"] == "team":
                raise ValueError(
                    f"{profile}/{item['job_id']}: direct-send script requires a reviewed code change"
                )
            if item["classification"] == "team":
                room = str(item["intended_room"])
                room_id = room_map.get(room)
                if not room_id:
                    raise ValueError(f"missing room UUID for {room}")
                destination = f"buzz:{room_id}"
                old_prompt = str(job.get("prompt") or "")
                new_prompt = _rewrite_prompt(
                    old_prompt,
                    room,
                    quiet_success=bool(item["quiet_success"]),
                )
                prompt_changed = old_prompt != new_prompt
                if prompt_changed:
                    job["prompt"] = new_prompt
                    profile_changed = True
            else:
                destination = "local"
                prompt_changed = False
            destination_changed = str(job.get("deliver") or "missing") != destination
            if destination_changed:
                job["deliver"] = destination
                profile_changed = True
            if prompt_changed or destination_changed:
                changes.append(
                    {
                        "profile": profile,
                        "job_id": item["job_id"],
                        "destination": destination,
                        "prompt_changed": prompt_changed,
                        "destination_changed": destination_changed,
                    }
                )
        if apply and profile_changed:
            _atomic_write_json(path, payload)
    return {
        "valid": True,
        "applied": apply,
        "changed_jobs": len(changes),
        "changes": changes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles-root", type=Path, required=True)
    parser.add_argument("--organization", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--topology", type=Path, required=True)
    parser.add_argument("--room-map", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verified-backup", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.apply:
            if args.verified_backup is None:
                raise ValueError("--apply requires --verified-backup")
            _verified_backup(args.verified_backup)
        report = migrate(
            profiles_root=args.profiles_root,
            organization=args.organization,
            policy=args.policy,
            topology=args.topology,
            room_map=_load_room_map(args.room_map),
            apply=args.apply,
        )
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
        report = {"valid": False, "applied": False, "error": str(exc)}
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
