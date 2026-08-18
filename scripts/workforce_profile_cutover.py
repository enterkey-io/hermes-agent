#!/usr/bin/env python3
"""Atomic whole-workforce AGENTS.md cutover and rollback.

The apply path fails closed on planned profiles, stale source/candidate hashes,
or an invalid full backup. It creates and verifies the required byte-for-byte
AGENTS-only archive immediately before writing any live profile.
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

from scripts.workforce_restore import _atomic_write, verify_backup


APPLY_TOKEN = "APPLY-WHOLE-WORKFORCE-PROFILE-CUTOVER"
ROLLBACK_TOKEN = "ROLLBACK-WHOLE-WORKFORCE-PROFILE-CUTOVER"


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def preflight(manifest_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = manifest.get("profiles")
    if not isinstance(rows, list) or not rows:
        raise ValueError("profile rewrite manifest is empty")
    prepared = []
    gates = []
    for row in rows:
        agent = str(row.get("agent") or "")
        status = str(row.get("status") or "")
        source = Path(str(row.get("source") or ""))
        candidate = Path(str(row.get("candidate") or ""))
        if status != "active":
            gates.append(f"{agent}: status is {status}, not active")
        if not source.is_file():
            gates.append(f"{agent}: live AGENTS.md is absent")
            continue
        if not candidate.is_file():
            gates.append(f"{agent}: candidate AGENTS.md is absent")
            continue
        source_bytes = source.read_bytes()
        candidate_bytes = candidate.read_bytes()
        if _sha_bytes(source_bytes) != row.get("source_sha256"):
            gates.append(f"{agent}: live source hash drifted")
        if _sha_bytes(candidate_bytes) != row.get("candidate_sha256"):
            gates.append(f"{agent}: candidate hash drifted")
        prepared.append(
            {
                "agent": agent,
                "source": source,
                "candidate": candidate,
                "source_bytes": source_bytes,
                "candidate_bytes": candidate_bytes,
                "source_mode": source.stat().st_mode & 0o777,
                "source_sha256": _sha_bytes(source_bytes),
                "candidate_sha256": _sha_bytes(candidate_bytes),
            }
        )
    report = {
        "valid": not gates and len(prepared) == len(rows),
        "applied": False,
        "profiles": len(rows),
        "writes_ready": len(prepared),
        "gates": gates,
        "expected_writes": [
            {
                "agent": item["agent"],
                "target": str(item["source"]),
                "source_sha256": item["source_sha256"],
                "candidate_sha256": item["candidate_sha256"],
            }
            for item in prepared
        ],
    }
    return report, prepared


def create_immediate_backup(output: Path, prepared: list[dict[str, Any]]) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(output, 0o700)
    archive_path = output / "operational-agents-before.tar"
    records = []
    with tarfile.open(archive_path, "w") as archive:
        for item in prepared:
            profile = item["source"].parent.name
            arcname = f"profiles/{profile}/AGENTS.md"
            archive.add(item["source"], arcname=arcname, recursive=False)
            records.append(
                {
                    "agent": item["agent"],
                    "target": str(item["source"]),
                    "archive_member": arcname,
                    "sha256": item["source_sha256"],
                    "mode": item["source_mode"],
                }
            )
    os.chmod(archive_path, 0o600)
    manifest = {"schema_version": 1, "files": records}
    manifest_path = output / "manifest.json"
    _atomic_write(manifest_path, (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(), 0o600)
    checksums = {
        archive_path.name: _sha_bytes(archive_path.read_bytes()),
        manifest_path.name: _sha_bytes(manifest_path.read_bytes()),
    }
    _atomic_write(
        output / "SHA256SUMS",
        "".join(f"{digest}  {name}\n" for name, digest in checksums.items()).encode(),
        0o600,
    )
    with tarfile.open(archive_path, "r") as archive:
        by_name = {member.name: member for member in archive.getmembers()}
        for record in records:
            member = by_name.get(record["archive_member"])
            source = archive.extractfile(member) if member else None
            if source is None or _sha_bytes(source.read()) != record["sha256"]:
                raise ValueError(f"immediate backup verification failed: {record['agent']}")
    restore_test = {"valid": True, "checked_files": len(records), "mismatches": []}
    _atomic_write(output / "restore-test.json", (json.dumps(restore_test, indent=2) + "\n").encode(), 0o600)
    return {"path": str(output), "files": len(records), "archive_sha256": checksums[archive_path.name]}


def apply_cutover(manifest_path: Path, verified_backup: Path, immediate_backup: Path) -> dict[str, Any]:
    verify_backup(verified_backup)
    report, prepared = preflight(manifest_path)
    if not report["valid"]:
        raise ValueError("profile cutover preflight failed: " + "; ".join(report["gates"]))
    backup_result = create_immediate_backup(immediate_backup, prepared)
    written = []
    try:
        for item in prepared:
            _atomic_write(item["source"], item["candidate_bytes"], item["source_mode"])
            written.append(item)
        for item in prepared:
            if _sha_bytes(item["source"].read_bytes()) != item["candidate_sha256"]:
                raise ValueError(f"post-write hash mismatch: {item['agent']}")
    except Exception:
        for item in written:
            _atomic_write(item["source"], item["source_bytes"], item["source_mode"])
        raise
    report["applied"] = True
    report["immediate_backup"] = backup_result
    return report


def rollback_cutover(immediate_backup: Path) -> dict[str, Any]:
    manifest_path = immediate_backup / "manifest.json"
    archive_path = immediate_backup / "operational-agents-before.tar"
    checksums = {}
    for line in (immediate_backup / "SHA256SUMS").read_text(encoding="ascii").splitlines():
        digest, name = line.split(maxsplit=1)
        checksums[name.strip()] = digest
    for path in (manifest_path, archive_path):
        if _sha_bytes(path.read_bytes()) != checksums.get(path.name):
            raise ValueError(f"immediate backup checksum failed: {path.name}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    restored = []
    with tarfile.open(archive_path, "r") as archive:
        by_name = {member.name: member for member in archive.getmembers()}
        payloads = []
        for record in manifest["files"]:
            member = by_name.get(record["archive_member"])
            source = archive.extractfile(member) if member else None
            if source is None:
                raise ValueError(f"missing immediate backup member: {record['archive_member']}")
            content = source.read()
            if _sha_bytes(content) != record["sha256"]:
                raise ValueError(f"immediate backup member hash failed: {record['agent']}")
            payloads.append((record, content))
        for record, content in payloads:
            target = Path(record["target"])
            _atomic_write(target, content, int(record["mode"]))
            restored.append({"agent": record["agent"], "target": str(target), "sha256": record["sha256"]})
    return {"valid": True, "applied": True, "restored": restored}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("preflight")
    check.add_argument("--manifest", type=Path, required=True)
    apply_parser = sub.add_parser("apply")
    apply_parser.add_argument("--manifest", type=Path, required=True)
    apply_parser.add_argument("--verified-backup", type=Path, required=True)
    apply_parser.add_argument("--immediate-backup", type=Path, required=True)
    apply_parser.add_argument("--confirm", required=True)
    rollback = sub.add_parser("rollback")
    rollback.add_argument("--immediate-backup", type=Path, required=True)
    rollback.add_argument("--confirm", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "preflight":
            report, _ = preflight(args.manifest)
        elif args.command == "apply":
            if args.confirm != APPLY_TOKEN:
                raise ValueError(f"apply requires --confirm {APPLY_TOKEN}")
            report = apply_cutover(args.manifest, args.verified_backup, args.immediate_backup)
        else:
            if args.confirm != ROLLBACK_TOKEN:
                raise ValueError(f"rollback requires --confirm {ROLLBACK_TOKEN}")
            report = rollback_cutover(args.immediate_backup)
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
