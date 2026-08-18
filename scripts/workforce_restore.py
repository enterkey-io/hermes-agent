#!/usr/bin/env python3
"""Validate or restore workforce AGENTS/Cron files from a verified backup.

This intentionally restores only instruction and scheduler files. It never
restores credentials, runtime state, memory, identity, or unrelated profile
content. Dry-run is the default.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tarfile
import tempfile
from typing import Any

from hermes_cli.workforce_org import load_organization


CONFIRM_TOKEN = "RESTORE-WHOLE-WORKFORCE-FILES"


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def verify_backup(backup: Path) -> None:
    restore = json.loads((backup / "restore-test.json").read_text(encoding="utf-8"))
    if restore.get("valid") is not True or restore.get("mismatches"):
        raise ValueError("backup restore test is not valid")
    expected: dict[str, str] = {}
    for line in (backup / "SHA256SUMS").read_text(encoding="ascii").splitlines():
        digest, name = line.split(maxsplit=1)
        expected[name.strip()] = digest
    for name in ("workforce-profiles.tar", "manifest.json"):
        path = backup / name
        if not path.is_file() or _sha(path.read_bytes()) != expected.get(name):
            raise ValueError(f"backup checksum failed: {name}")


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def restore(
    *,
    backup: Path,
    organization: Path,
    profiles_root: Path,
    scope: str,
    apply: bool,
) -> dict[str, Any]:
    verify_backup(backup)
    org = load_organization(organization)
    suffixes = ["AGENTS.md"] if scope == "instructions" else ["cron/jobs.json"] if scope == "delivery" else ["AGENTS.md", "cron/jobs.json"]
    targets = []
    missing_baseline = []
    with tarfile.open(backup / "workforce-profiles.tar", "r") as archive:
        by_name = {member.name.removeprefix("./"): member for member in archive.getmembers()}
        for agent in org.operational_agents():
            profile_name = Path(agent.profile_path or agent.agent).name
            found = False
            for suffix in suffixes:
                member_name = f"profiles/{profile_name}/{suffix}"
                member = by_name.get(member_name)
                if member is None or not member.isfile():
                    continue
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError(f"cannot read backup member {member_name}")
                content = source.read()
                target = profiles_root / profile_name / suffix
                targets.append(
                    {
                        "agent": agent.agent,
                        "target": target,
                        "content": content,
                        "mode": member.mode,
                        "sha256": _sha(content),
                    }
                )
                found = True
            if not found:
                missing_baseline.append(agent.agent)
    report = {
        "valid": True,
        "applied": False,
        "scope": scope,
        "restore_files": [
            {"agent": item["agent"], "target": str(item["target"]), "sha256": item["sha256"]}
            for item in targets
        ],
        "profiles_without_pre_cutover_files": missing_baseline,
    }
    unexpected = [name for name in missing_baseline if org.get(name).status != "planned"]
    if unexpected:
        raise ValueError(f"active profiles missing backup files: {', '.join(unexpected)}")
    if not apply:
        return report

    before: dict[Path, tuple[bytes | None, int]] = {}
    try:
        for item in targets:
            target = item["target"]
            before[target] = (
                target.read_bytes() if target.is_file() else None,
                target.stat().st_mode & 0o777 if target.exists() else 0o600,
            )
            _atomic_write(target, item["content"], item["mode"])
        for item in targets:
            if _sha(item["target"].read_bytes()) != item["sha256"]:
                raise ValueError(f"post-restore hash mismatch: {item['target']}")
    except Exception:
        for target, (content, mode) in before.items():
            if content is not None:
                _atomic_write(target, content, mode)
        raise
    report["applied"] = True
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--organization", type=Path, required=True)
    parser.add_argument("--profiles-root", type=Path, required=True)
    parser.add_argument("--scope", choices=("instructions", "delivery", "all"), default="all")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.apply and args.confirm != CONFIRM_TOKEN:
            raise ValueError(f"--apply requires --confirm {CONFIRM_TOKEN}")
        report = restore(
            backup=args.backup,
            organization=args.organization,
            profiles_root=args.profiles_root,
            scope=args.scope,
            apply=args.apply,
        )
    except (OSError, ValueError, json.JSONDecodeError, tarfile.TarError) as exc:
        report = {"valid": False, "applied": False, "error": str(exc)}
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        args.output.chmod(0o600)
    print(rendered, end="")
    return 0 if report.get("valid") else 1


if __name__ == "__main__":
    raise SystemExit(main())
