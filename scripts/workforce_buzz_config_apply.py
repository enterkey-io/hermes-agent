#!/usr/bin/env python3
"""Apply or roll back a reviewed workforce Buzz configuration plan."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Callable

from scripts.workforce_host_delivery_migrate import verify_backup


APPLY_CONFIRM = "APPLY-WORKFORCE-BUZZ-PROFILE-CONFIG"
ROLLBACK_CONFIRM = "ROLLBACK-WORKFORCE-BUZZ-PROFILE-CONFIG"
ALLOWED_KEYS = {
    "gateway.platforms.buzz.extra.channels",
    "gateway.platforms.buzz.extra.home_channel",
    "gateway.platforms.buzz.extra.require_mention",
}


def load_plan(path: Path) -> dict[str, Any]:
    plan = json.loads(path.read_text(encoding="utf-8"))
    if plan.get("valid") is not True or plan.get("mutation_performed") is not False:
        raise ValueError("Buzz plan is not a valid non-executed plan")
    profiles = plan.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        raise ValueError("Buzz plan has no profiles")
    names = [item.get("profile") for item in profiles]
    if len(names) != len(set(names)) or any(not name for name in names):
        raise ValueError("Buzz plan profile names must be unique and non-empty")
    for item in profiles:
        home = item.get("environment", {}).get("HERMES_HOME")
        if not isinstance(home, str) or not Path(home).is_absolute():
            raise ValueError(f"invalid HERMES_HOME for {item.get('profile')}")
        for field in ("commands_not_executed", "inverse_commands_not_executed"):
            commands = item.get(field)
            if not isinstance(commands, list) or len(commands) != 3:
                raise ValueError(f"invalid {field} for {item.get('profile')}")
            for command in commands:
                if (
                    not isinstance(command, list)
                    or len(command) not in (4, 5)
                    or command[:2] != ["hermes", "config"]
                    or command[2] not in ("set", "unset")
                    or command[3] not in ALLOWED_KEYS
                    or (command[2] == "set" and len(command) != 5)
                    or (command[2] == "unset" and len(command) != 4)
                ):
                    raise ValueError(f"unsafe Buzz config command for {item.get('profile')}")
    return plan


def execute_plan(
    plan: dict[str, Any],
    *,
    hermes_bin: Path,
    rollback: bool,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    if not hermes_bin.is_file():
        raise ValueError("Hermes executable is absent")
    field = "inverse_commands_not_executed" if rollback else "commands_not_executed"
    profiles = list(reversed(plan["profiles"])) if rollback else plan["profiles"]
    executed: list[tuple[dict[str, Any], list[str]]] = []
    try:
        for profile in profiles:
            environment = os.environ.copy()
            environment.update(profile["environment"])
            for command in profile[field]:
                argv = [str(hermes_bin), *command[1:]]
                runner(
                    argv,
                    env=environment,
                    check=True,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                executed.append((profile, command))
    except Exception:
        if not rollback:
            completed_profiles = []
            seen = set()
            for profile, _command in executed:
                if profile["profile"] not in seen:
                    seen.add(profile["profile"])
                    completed_profiles.append(profile)
            for profile in reversed(completed_profiles):
                environment = os.environ.copy()
                environment.update(profile["environment"])
                for command in profile["inverse_commands_not_executed"]:
                    runner(
                        [str(hermes_bin), *command[1:]],
                        env=environment,
                        check=True,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                    )
        raise
    return {
        "valid": True,
        "applied": not rollback,
        "rolled_back": rollback,
        "mutation_performed": True,
        "profile_count": len(profiles),
        "command_count": len(executed),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--hermes-bin", type=Path, required=True)
    parser.add_argument("--verified-backup", type=Path)
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--confirm", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.rollback:
        if args.confirm != ROLLBACK_CONFIRM:
            raise SystemExit(f"refusing rollback: --confirm must be {ROLLBACK_CONFIRM}")
    else:
        if args.confirm != APPLY_CONFIRM:
            raise SystemExit(f"refusing apply: --confirm must be {APPLY_CONFIRM}")
        if args.verified_backup is None:
            raise SystemExit("refusing apply: --verified-backup is required")
        verify_backup(args.verified_backup)
    plan = load_plan(args.plan)
    report = execute_plan(
        plan,
        hermes_bin=args.hermes_bin,
        rollback=args.rollback,
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.report.chmod(0o600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
