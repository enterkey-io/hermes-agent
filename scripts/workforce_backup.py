#!/usr/bin/env python3
"""Create and verify an owner-only, credential-free workforce backup."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import tarfile
import tempfile
from pathlib import Path
from typing import Any
import shutil

import yaml


TOP_LEVEL_FILES = {
    "AGENTS.md", "SOUL.md", "identity.md", "user.md", "TOOLS.md",
    "channel_directory.json",
}
INCLUDED_TREES = {"memories", "skills", "assets", "baselines", "photo-guidance"}
EXCLUDED_NAMES = {
    ".env", ".op.env", "auth.json", "google_token.json", "state.db",
    "sessions", "conversations", "logs", "cache", "media", "audio_cache",
    "image_cache", "pending_messages", "pairing", "home", "runtime",
}
SAFE_CONFIG_PATHS = {
    "model", "toolsets", "agent.reasoning_effort", "agent.max_turns",
    "agent.service_tier", "timezone", "memory", "approvals.mode",
    "approvals.cron_mode", "cron.wrap_response", "kanban", "platform_models",
    "plugins.enabled", "plugins.disabled", "display.platforms.buzz",
    "gateway.platforms.buzz.enabled", "gateway.platforms.buzz.extra.channels",
    "gateway.platforms.buzz.extra.home_channel",
    "gateway.platforms.buzz.extra.require_mention",
    "delegation.model", "delegation.provider", "delegation.reasoning_effort",
    "delegation.max_iterations", "delegation.max_concurrent_children",
    "delegation.max_spawn_depth", "delegation.child_timeout_seconds",
    "delegation.subagent_auto_approve", "delegation.orchestrator_enabled",
    "delegation.default_toolsets", "delegation.inherit_mcp_toolsets",
    "delegation.max_summary_chars",
    "auxiliary.background_review.enabled",
    "auxiliary.background_review.provider",
    "auxiliary.background_review.model",
    "auxiliary.background_review.reasoning_effort",
    "auxiliary.background_review.timeout",
}

for _auxiliary_task in (
    "compression", "curator", "flush_memories", "kanban_decomposer", "mcp",
    "monitor", "profile_describer", "session_search", "skills_hub",
    "title_generation", "triage_specifier", "tts_audio_tags", "vision",
    "web_extract",
):
    SAFE_CONFIG_PATHS.update(
        {
            f"auxiliary.{_auxiliary_task}.enabled",
            f"auxiliary.{_auxiliary_task}.provider",
            f"auxiliary.{_auxiliary_task}.model",
            f"auxiliary.{_auxiliary_task}.reasoning_effort",
            f"auxiliary.{_auxiliary_task}.timeout",
        }
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _select(config: dict[str, Any], dotted: str) -> Any:
    value: Any = config
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _assign(target: dict[str, Any], dotted: str, value: Any) -> None:
    cursor = target
    parts = dotted.split(".")
    for part in parts[:-1]:
        cursor = cursor.setdefault(part, {})
    cursor[parts[-1]] = value


def sanitized_config(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    if not isinstance(raw, dict):
        return {}
    safe: dict[str, Any] = {}
    for dotted in sorted(SAFE_CONFIG_PATHS):
        value = _select(raw, dotted)
        if value is not None:
            _assign(safe, dotted, value)
    return safe


def selected_paths(profile: Path) -> list[Path]:
    paths = [profile / name for name in sorted(TOP_LEVEL_FILES) if (profile / name).is_file()]
    jobs = profile / "cron" / "jobs.json"
    if jobs.is_file():
        paths.append(jobs)
    for name in sorted(INCLUDED_TREES):
        tree = profile / name
        if tree.exists() or tree.is_symlink():
            paths.append(tree)
    return paths


def _copytree_ignore(_directory: str, names: list[str]) -> set[str]:
    """Exclude credential/volatile basenames at every copied tree depth."""
    return {name for name in names if name in EXCLUDED_NAMES}


def _manifest_tree(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        info = path.lstat()
        record = {
            "path": rel,
            "mode": stat.S_IMODE(info.st_mode),
            "mtime_ns": info.st_mtime_ns,
            "type": "symlink" if path.is_symlink() else "directory" if path.is_dir() else "file",
        }
        if path.is_symlink():
            record["target"] = os.readlink(path)
        elif path.is_file():
            record["size"] = info.st_size
            record["sha256"] = _sha256(path)
        records.append(record)
    return records


def create_backup(profiles_root: Path, output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(output_dir, 0o700)
    with tempfile.TemporaryDirectory(prefix="workforce-backup-") as tmp_name:
        stage = Path(tmp_name) / "payload"
        stage.mkdir(mode=0o700)
        stage_profiles = stage / "profiles"
        stage_profiles.mkdir(mode=0o700)
        profile_names = []
        for profile in sorted(path for path in profiles_root.iterdir() if path.is_dir()):
            profile_names.append(profile.name)
            destination = stage_profiles / profile.name
            destination.mkdir(mode=0o700)
            for source in selected_paths(profile):
                target = destination / source.relative_to(profile)
                target.parent.mkdir(parents=True, exist_ok=True)
                if source.is_dir() and not source.is_symlink():
                    shutil.copytree(
                        source,
                        target,
                        symlinks=True,
                        copy_function=shutil.copy2,
                        ignore=_copytree_ignore,
                    )
                elif source.is_symlink():
                    target.symlink_to(os.readlink(source))
                else:
                    shutil.copy2(source, target, follow_symlinks=False)
            config = profile / "config.yaml"
            if config.is_file():
                safe_path = destination / "config.non-secret.yaml"
                safe_path.write_text(
                    yaml.safe_dump(sanitized_config(config), sort_keys=True), encoding="utf-8"
                )
                os.chmod(safe_path, 0o600)

        records = _manifest_tree(stage)
        manifest = {
            "schema_version": 1,
            "profiles": profile_names,
            "profile_count": len(profile_names),
            "included": sorted(TOP_LEVEL_FILES | INCLUDED_TREES | {"cron/jobs.json", "config.non-secret.yaml"}),
            "excluded_secret_or_volatile_names": sorted(EXCLUDED_NAMES),
            "files": records,
        }
        manifest_path = output_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(manifest_path, 0o600)
        archive = output_dir / "workforce-profiles.tar"
        with tarfile.open(archive, "w", dereference=False) as tar:
            tar.add(stage, arcname=".", recursive=True)
        os.chmod(archive, 0o600)
        archive_hash = _sha256(archive)
        hash_path = output_dir / "SHA256SUMS"
        hash_path.write_text(
            f"{archive_hash}  {archive.name}\n{_sha256(manifest_path)}  {manifest_path.name}\n",
            encoding="ascii",
        )
        os.chmod(hash_path, 0o600)
    return {
        "output_dir": str(output_dir), "archive": str(archive),
        "archive_sha256": archive_hash, "profile_count": len(profile_names),
    }


def verify_backup(output_dir: Path, scratch: Path) -> dict[str, Any]:
    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    archive = output_dir / "workforce-profiles.tar"
    scratch.mkdir(parents=True, exist_ok=False, mode=0o700)
    with tarfile.open(archive, "r") as tar:
        # This archive was produced locally from an explicit allowlist. Some
        # protected skill trees intentionally contain absolute symlinks to
        # their profile-local runtimes. Preserve the link text without
        # following it; the post-extraction manifest verifies every target.
        tar.extractall(scratch, filter="fully_trusted")
    actual = {item["path"]: item for item in _manifest_tree(scratch)}
    mismatches = []
    for expected in manifest["files"]:
        observed = actual.get(expected["path"])
        if observed is None:
            mismatches.append({"path": expected["path"], "reason": "missing"})
            continue
        for key in ("type", "size", "sha256", "target"):
            if expected.get(key) != observed.get(key):
                mismatches.append({"path": expected["path"], "reason": key})
    result = {
        "valid": not mismatches,
        "checked_records": len(manifest["files"]),
        "mismatches": mismatches,
        "scratch": str(scratch),
    }
    report = output_dir / "restore-test.json"
    report.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(report, 0o600)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--profiles-root", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--backup", type=Path, required=True)
    verify.add_argument("--scratch", type=Path, required=True)
    args = parser.parse_args()
    result = create_backup(args.profiles_root, args.output) if args.command == "create" else verify_backup(args.backup, args.scratch)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("valid", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
