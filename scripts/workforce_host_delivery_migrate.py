#!/usr/bin/env python3
"""Stage or apply host-cron and Xenia failure delivery migration to Buzz.

Dry-run is the default. Live application requires a verified workforce backup,
an empty rollback directory, a complete room UUID map, and an explicit cutover
confirmation token. The transaction restores the Xenia script if crontab
installation fails.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any
import uuid

import yaml


CONFIRM_TOKEN = "APPLY-WORKFORCE-HOST-DELIVERY-CUTOVER"
REQUIRED_ROOMS = {"director-operations", "director-trading", "executive-support"}
XENIA_PATH = Path("/home/elliott/.hermes/scripts/xenia-tradestation-token-refresh.sh")
HERMES_BIN = "/home/elliott/.hermes/hermes-agent/venv/bin/hermes"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def load_room_map(path: Path) -> dict[str, str]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    if not isinstance(raw, dict):
        raise ValueError("room map must be a mapping")
    result = {str(name): str(value) for name, value in raw.items()}
    missing = sorted(REQUIRED_ROOMS - result.keys())
    if missing:
        raise ValueError(f"room map is missing: {', '.join(missing)}")
    for name in REQUIRED_ROOMS:
        try:
            uuid.UUID(result[name])
        except ValueError as exc:
            raise ValueError(f"{name}: room id is not a UUID") from exc
    if len({result[name] for name in REQUIRED_ROOMS}) != len(REQUIRED_ROOMS):
        raise ValueError("required rooms must have distinct UUIDs")
    return result


def verify_backup(path: Path) -> None:
    result = json.loads((path / "restore-test.json").read_text(encoding="utf-8"))
    if result.get("valid") is not True or result.get("mismatches"):
        raise ValueError("workforce backup restoration test is not valid")
    if not (path / "workforce-profiles.tar").is_file() or not (path / "SHA256SUMS").is_file():
        raise ValueError("verified workforce backup is incomplete")


def _inject_env(line: str, marker: str, room_id: str) -> str:
    prefix = f"WORKFORCE_BUZZ_ROOM_ID={room_id} "
    if prefix in line:
        return line
    if marker not in line:
        raise ValueError(f"expected command marker is absent: {marker}")
    return line.replace(marker, prefix + marker, 1)


def _replace_inline_telegram(line: str, room_id: str) -> str:
    rewritten, count = re.subn(r"--to\s+telegram\b", f"--to buzz:{room_id}", line)
    if count != 1:
        raise ValueError("expected exactly one inline Telegram destination")
    return rewritten


def _replace_elliott_msg(line: str, room_id: str) -> str:
    pattern = re.compile(
        r"/home/elliott/nanoclaw/scripts/shared/elliott-msg\.sh\s+"
        r"--as\s+root\s+--to\s+enterkey\s+"
    )
    replacement = f"{HERMES_BIN} send --to buzz:{room_id} --quiet "
    rewritten, count = pattern.subn(replacement, line)
    if count != 1:
        raise ValueError("expected exactly one elliott-msg fallback")
    return rewritten


def transform_crontab(text: str, rooms: dict[str, str]) -> tuple[str, list[dict[str, Any]]]:
    changes: list[dict[str, Any]] = []
    output: list[str] = []
    seen: set[str] = set()
    for raw_line in text.splitlines(keepends=True):
        newline = "\n" if raw_line.endswith("\n") else ""
        line = raw_line[:-1] if newline else raw_line
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", stripped):
            output.append(raw_line)
            continue

        rewritten = line
        route = None
        if "xenia-tradestation-token-refresh.sh" in line:
            route = "xenia-token-refresh"
            rewritten = _inject_env(line, "/home/elliott/.hermes/scripts/xenia-tradestation-token-refresh.sh", rooms["director-trading"])
        elif "refresh_token.py" in line:
            route = "m365-token-refresh"
            if "--to telegram" in line:
                rewritten = _replace_inline_telegram(line, rooms["executive-support"])
            elif f"--to buzz:{rooms['executive-support']}" not in line:
                raise ValueError("M365 refresh has an unexpected failure destination")
        elif "refresh_google_accounts.py" in line:
            route = "google-token-refresh"
            if "--to telegram" in line:
                rewritten = _replace_inline_telegram(line, rooms["executive-support"])
            elif f"--to buzz:{rooms['executive-support']}" not in line:
                raise ValueError("Google refresh has an unexpected failure destination")
        elif "weekly-security-audit.sh" in line:
            route = "weekly-security-audit"
            rewritten = _inject_env(line, "bash /home/elliott/nanoclaw/scripts/shared/weekly-security-audit.sh", rooms["director-operations"])
        elif "cron-monthly-disk-review.sh" in line:
            route = "monthly-disk-review"
            rewritten = _inject_env(line, "/home/elliott/nanoclaw/scripts/shared/cron-monthly-disk-review.sh", rooms["director-operations"])
        elif "move-logs-to-daily.sh" in line:
            route = "move-logs-to-daily"
            if "elliott-msg.sh" in line:
                rewritten = _replace_elliott_msg(line, rooms["director-operations"])
            elif f"--to buzz:{rooms['director-operations']}" not in line:
                raise ValueError("memory log sweep has an unexpected failure destination")
        elif "archive-sessions.sh" in line:
            route = "archive-sessions"
            if "elliott-msg.sh" in line:
                rewritten = _replace_elliott_msg(line, rooms["director-operations"])
            elif f"--to buzz:{rooms['director-operations']}" not in line:
                raise ValueError("session archive has an unexpected failure destination")
        elif "/nanoclaw/watcher/watcher.py" in line:
            route = "nanoclaw-watcher"
            rewritten = _inject_env(line, "/usr/bin/python3 /home/elliott/nanoclaw/watcher/watcher.py", rooms["director-operations"])
        elif "op-onecli-sync.sh" in line:
            route = "op-onecli-sync"
            rewritten = _inject_env(line, "/home/elliott/nanoclaw/scripts/shared/op-onecli-sync.sh", rooms["director-operations"])

        if route:
            if route in seen:
                raise ValueError(f"duplicate active host route: {route}")
            seen.add(route)
            changes.append(
                {
                    "route": route,
                    "before_sha256": sha256_bytes(line.encode()),
                    "after_sha256": sha256_bytes(rewritten.encode()),
                }
            )
        output.append(rewritten + newline)

    expected = {
        "xenia-token-refresh",
        "m365-token-refresh",
        "google-token-refresh",
        "weekly-security-audit",
        "monthly-disk-review",
        "move-logs-to-daily",
        "archive-sessions",
        "nanoclaw-watcher",
        "op-onecli-sync",
    }
    missing = sorted(expected - seen)
    if missing:
        raise ValueError(f"active host routes disappeared: {', '.join(missing)}")
    return "".join(output), changes


def transform_xenia_script(text: str) -> str:
    if "--to telegram" not in text and 'buzz:${WORKFORCE_BUZZ_ROOM_ID}' in text:
        return text
    if text.count("--to telegram") != 1:
        raise ValueError("Xenia script must contain exactly one Telegram failure route")
    anchor = "umask 077\n"
    if anchor not in text:
        raise ValueError("Xenia script umask anchor is absent")
    text = text.replace(
        anchor,
        anchor
        + 'WORKFORCE_BUZZ_ROOM_ID="${WORKFORCE_BUZZ_ROOM_ID:-}"\n',
        1,
    )
    notify_anchor = "  if HERMES_HOME=\"$PROFILE_ROOT\" \\\n"
    if notify_anchor not in text:
        raise ValueError("Xenia notification anchor is absent")
    text = text.replace(
        notify_anchor,
        '  if [[ ! "$WORKFORCE_BUZZ_ROOM_ID" =~ ^[0-9a-fA-F-]{36}$ ]]; then\n'
        '    printf "%s Buzz room UUID unavailable; failure retained in protected log\\n" "$(date --iso-8601=seconds)" >> "$LOG"\n'
        "    return 0\n"
        "  fi\n\n"
        + notify_anchor,
        1,
    )
    return text.replace("--to telegram --quiet", '--to "buzz:${WORKFORCE_BUZZ_ROOM_ID}" --quiet', 1)


def _atomic_write(path: Path, content: str, mode: int) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
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


def _prepare_rollback(rollback_dir: Path, crontab: str, xenia_path: Path) -> dict[str, Any]:
    if rollback_dir.exists() and any(rollback_dir.iterdir()):
        raise ValueError("rollback directory must not exist or must be empty")
    rollback_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(rollback_dir, 0o700)
    cron_path = rollback_dir / "crontab.before"
    xenia_backup = rollback_dir / "xenia-tradestation-token-refresh.sh.before"
    _atomic_write(cron_path, crontab, 0o600)
    shutil.copy2(xenia_path, xenia_backup)
    os.chmod(xenia_backup, 0o600)
    manifest = {
        "schema_version": 1,
        "crontab": {"path": str(cron_path), "sha256": sha256_bytes(crontab.encode())},
        "xenia_script": {"path": str(xenia_backup), "sha256": sha256_bytes(xenia_backup.read_bytes())},
    }
    _atomic_write(rollback_dir / "manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n", 0o600)
    return manifest


def migrate(
    *,
    crontab_text: str,
    xenia_path: Path,
    rooms: dict[str, str],
    apply: bool,
    rollback_dir: Path | None = None,
) -> dict[str, Any]:
    candidate_crontab, changes = transform_crontab(crontab_text, rooms)
    current_xenia = xenia_path.read_text(encoding="utf-8")
    candidate_xenia = transform_xenia_script(current_xenia)
    report: dict[str, Any] = {
        "valid": True,
        "applied": False,
        "host_schedule_changes": changes,
        "xenia_before_sha256": sha256_bytes(current_xenia.encode()),
        "xenia_after_sha256": sha256_bytes(candidate_xenia.encode()),
        "candidate_crontab_sha256": sha256_bytes(candidate_crontab.encode()),
        "legacy_active_delivery_markers_remaining": [],
    }
    active = "\n".join(
        line for line in candidate_crontab.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    for marker in ("--to telegram", "elliott-msg.sh --as root --to enterkey"):
        if marker in active:
            report["legacy_active_delivery_markers_remaining"].append(marker)
    if report["legacy_active_delivery_markers_remaining"]:
        raise ValueError("legacy active delivery marker remains after transformation")
    if not apply:
        return report
    if rollback_dir is None:
        raise ValueError("apply requires a rollback directory")
    rollback = _prepare_rollback(rollback_dir, crontab_text, xenia_path)
    try:
        _atomic_write(xenia_path, candidate_xenia, xenia_path.stat().st_mode & 0o777)
        subprocess.run(
            ["crontab", "-"], input=candidate_crontab, text=True,
            encoding="utf-8", errors="replace", check=True,
        )
    except Exception:
        shutil.copy2(rollback_dir / "xenia-tradestation-token-refresh.sh.before", xenia_path)
        raise
    report["applied"] = True
    report["rollback"] = rollback
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--room-map", type=Path, required=True)
    parser.add_argument("--input", type=Path, help="read a crontab fixture instead of live crontab")
    parser.add_argument("--xenia-script", type=Path, default=XENIA_PATH)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verified-backup", type=Path)
    parser.add_argument("--rollback-dir", type=Path)
    parser.add_argument("--confirm")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        rooms = load_room_map(args.room_map)
        if args.input:
            crontab_text = args.input.read_text(encoding="utf-8")
        else:
            proc = subprocess.run(
                ["crontab", "-l"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", check=True,
            )
            crontab_text = proc.stdout
        if args.apply:
            if args.input:
                raise ValueError("--apply cannot be combined with --input")
            if args.confirm != CONFIRM_TOKEN:
                raise ValueError(f"--apply requires --confirm {CONFIRM_TOKEN}")
            if args.verified_backup is None:
                raise ValueError("--apply requires --verified-backup")
            verify_backup(args.verified_backup)
        report = migrate(
            crontab_text=crontab_text,
            xenia_path=args.xenia_script,
            rooms=rooms,
            apply=args.apply,
            rollback_dir=args.rollback_dir,
        )
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError, subprocess.CalledProcessError) as exc:
        report = {"valid": False, "applied": False, "error": str(exc)}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(args.report, json.dumps(report, indent=2, sort_keys=True) + "\n", 0o600)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report.get("valid") else 1


if __name__ == "__main__":
    raise SystemExit(main())
