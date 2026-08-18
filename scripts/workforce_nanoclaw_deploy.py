#!/usr/bin/env python3
"""Plan, apply, or roll back the isolated NanoClaw delivery patch bundle.

Application is deliberately hash-guarded: the tested candidate files and the
dirty production files must still match the reviewed plan.  No git operation
is performed against the production checkout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any


FILES = (
    "scripts/shared/weekly-security-audit.sh",
    "scripts/shared/cron-monthly-disk-review.sh",
    "scripts/shared/op-onecli-sync.sh",
    "watcher/watcher.py",
)
APPLY_CONFIRM = "APPLY-NANOCLAW-WORKFORCE-DELIVERY-BUNDLE"
ROLLBACK_CONFIRM = "ROLLBACK-NANOCLAW-WORKFORCE-DELIVERY-BUNDLE"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_write(path: Path, value: bytes, mode: int) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def verify_workforce_backup(path: Path) -> None:
    result = json.loads((path / "restore-test.json").read_text(encoding="utf-8"))
    if result.get("valid") is not True or result.get("mismatches"):
        raise ValueError("workforce backup restoration test is not valid")
    if not (path / "workforce-profiles.tar").is_file() or not (path / "SHA256SUMS").is_file():
        raise ValueError("verified workforce backup is incomplete")


def build_plan(candidate_root: Path, target_root: Path) -> dict[str, Any]:
    entries = []
    for relative in FILES:
        candidate = candidate_root / relative
        target = target_root / relative
        if not candidate.is_file() or not target.is_file():
            raise ValueError(f"candidate and target must both exist: {relative}")
        candidate_bytes = candidate.read_bytes()
        target_bytes = target.read_bytes()
        entries.append(
            {
                "relative_path": relative,
                "candidate_sha256": sha256_bytes(candidate_bytes),
                "target_before_sha256": sha256_bytes(target_bytes),
                "candidate_mode": candidate.stat().st_mode & 0o777,
                "target_mode": target.stat().st_mode & 0o777,
                "change_required": candidate_bytes != target_bytes,
            }
        )
    return {
        "schema_version": 1,
        "valid": True,
        "applied": False,
        "candidate_root": str(candidate_root.resolve()),
        "target_root": str(target_root.resolve()),
        "files": entries,
        "file_count": len(entries),
        "mutation_performed": False,
    }


def _validate_expected(current: dict[str, Any], expected: dict[str, Any]) -> None:
    for key in ("candidate_root", "target_root", "file_count"):
        if current.get(key) != expected.get(key):
            raise ValueError(f"reviewed plan no longer matches: {key}")
    current_files = {
        item["relative_path"]: item for item in current.get("files", [])
    }
    expected_files = {
        item["relative_path"]: item for item in expected.get("files", [])
    }
    if current_files.keys() != expected_files.keys():
        raise ValueError("reviewed plan file set no longer matches")
    for relative, item in current_files.items():
        previous = expected_files[relative]
        for key in ("candidate_sha256", "target_before_sha256"):
            if item.get(key) != previous.get(key):
                raise ValueError(f"reviewed hash changed for {relative}: {key}")


def _prepare_rollback(plan: dict[str, Any], rollback_dir: Path) -> dict[str, Any]:
    if rollback_dir.exists() and any(rollback_dir.iterdir()):
        raise ValueError("rollback directory must not exist or must be empty")
    rollback_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(rollback_dir, 0o700)
    target_root = Path(plan["target_root"])
    records = []
    for entry in plan["files"]:
        relative = entry["relative_path"]
        source = target_root / relative
        backup = rollback_dir / "files" / relative
        backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copy2(source, backup)
        os.chmod(backup, 0o600)
        records.append(
            {
                "relative_path": relative,
                "backup_path": str(backup),
                "before_sha256": entry["target_before_sha256"],
                "after_sha256": entry["candidate_sha256"],
                "before_mode": entry["target_mode"],
            }
        )
    manifest = {
        "schema_version": 1,
        "target_root": plan["target_root"],
        "files": records,
    }
    _atomic_write(
        rollback_dir / "manifest.json",
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(),
        0o600,
    )
    return manifest


def apply_bundle(
    *,
    candidate_root: Path,
    target_root: Path,
    expected_plan: dict[str, Any],
    rollback_dir: Path,
) -> dict[str, Any]:
    plan = build_plan(candidate_root, target_root)
    _validate_expected(plan, expected_plan)
    rollback = _prepare_rollback(plan, rollback_dir)
    installed: list[dict[str, Any]] = []
    try:
        for entry in plan["files"]:
            relative = entry["relative_path"]
            target = target_root / relative
            _atomic_write(
                target,
                (candidate_root / relative).read_bytes(),
                entry["candidate_mode"],
            )
            installed.append(entry)
    except Exception:
        for entry in reversed(installed):
            record = next(
                item for item in rollback["files"]
                if item["relative_path"] == entry["relative_path"]
            )
            _atomic_write(
                target_root / entry["relative_path"],
                Path(record["backup_path"]).read_bytes(),
                record["before_mode"],
            )
        raise
    plan["applied"] = True
    plan["mutation_performed"] = True
    plan["rollback_dir"] = str(rollback_dir)
    return plan


def rollback_bundle(rollback_dir: Path) -> dict[str, Any]:
    manifest = json.loads((rollback_dir / "manifest.json").read_text(encoding="utf-8"))
    target_root = Path(manifest["target_root"])
    for entry in manifest["files"]:
        target = target_root / entry["relative_path"]
        if sha256_bytes(target.read_bytes()) != entry["after_sha256"]:
            raise ValueError(f"live file changed after cutover: {entry['relative_path']}")
    for entry in manifest["files"]:
        _atomic_write(
            target_root / entry["relative_path"],
            Path(entry["backup_path"]).read_bytes(),
            entry["before_mode"],
        )
    return {"valid": True, "rolled_back": True, "file_count": len(manifest["files"])}


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, (json.dumps(report, indent=2, sort_keys=True) + "\n").encode(), 0o600)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subparsers = result.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan")
    plan.add_argument("--candidate-root", type=Path, required=True)
    plan.add_argument("--target-root", type=Path, required=True)
    plan.add_argument("--output", type=Path, required=True)
    apply = subparsers.add_parser("apply")
    apply.add_argument("--candidate-root", type=Path, required=True)
    apply.add_argument("--target-root", type=Path, required=True)
    apply.add_argument("--expected-plan", type=Path, required=True)
    apply.add_argument("--verified-backup", type=Path, required=True)
    apply.add_argument("--rollback-dir", type=Path, required=True)
    apply.add_argument("--output", type=Path, required=True)
    apply.add_argument("--confirm", required=True)
    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--rollback-dir", type=Path, required=True)
    rollback.add_argument("--output", type=Path, required=True)
    rollback.add_argument("--confirm", required=True)
    return result


def main() -> int:
    args = parser().parse_args()
    if args.command == "plan":
        report = build_plan(args.candidate_root, args.target_root)
    elif args.command == "apply":
        if args.confirm != APPLY_CONFIRM:
            raise SystemExit(f"refusing apply: --confirm must be {APPLY_CONFIRM}")
        verify_workforce_backup(args.verified_backup)
        expected = json.loads(args.expected_plan.read_text(encoding="utf-8"))
        report = apply_bundle(
            candidate_root=args.candidate_root,
            target_root=args.target_root,
            expected_plan=expected,
            rollback_dir=args.rollback_dir,
        )
    else:
        if args.confirm != ROLLBACK_CONFIRM:
            raise SystemExit(f"refusing rollback: --confirm must be {ROLLBACK_CONFIRM}")
        report = rollback_bundle(args.rollback_dir)
    _write_report(args.output, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
