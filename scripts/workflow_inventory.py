#!/usr/bin/env python3
"""Read-only workflow and Paperclip dependency inventory for Runbook Registry.

The scanner deliberately records metadata, paths, counts, hashes, and redacted
evidence snippets. It must not copy credential values into reports.
"""

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_NAMES = (
    "workflow-inventory.json",
    "workflow-inventory.md",
    "paperclip-active-dependencies.json",
    "schedule-collision-report.json",
    "paperclip-export-reconciliation.json",
    "notification-path-inventory.json",
    "active-automation-authority-map.md",
)

SECRET_KEY_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password|passwd|authorization|bearer|cookie|credential)\b"
)
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password|passwd|authorization|bearer|cookie|credential)"
    r"(\s*[:=]\s*)(['\"]?)[^'\"\s,;]+"
)
ENV_SECRET_ASSIGNMENT_RE = re.compile(
    r"\b([A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)[A-Z0-9_]*"
    r"\s*=\s*)[^'\"\s,;\\]+"
)
KNOWN_TOKEN_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{2,}|ghp_[A-Za-z0-9_]{4,}|xox[baprs]-[A-Za-z0-9-]{4,})\b"
)
FAKE_SECRET_RE = re.compile(r"\b(?:secret|token|password|passwd)-[A-Za-z0-9_-]{3,}\b", re.I)
LONG_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_+=:]{40,}\b")
KEYWORDS = (
    "paperclip",
    "runbook",
    "evernote",
    "n8n",
    "sim",
    "cron",
    "systemd",
    "telegram",
    "matrix",
    "photon",
    "imessage",
    "email",
    "webhook",
)
ACTIVE_FILE_NAMES = {
    "AGENTS.md",
    "SYSTEM.md",
    "CLAUDE.md",
    "config.yaml",
    "config.yml",
    "jobs.json",
    "package.json",
}
TEXT_SUFFIXES = {
    ".cfg",
    ".conf",
    ".env",
    ".json",
    ".js",
    ".md",
    ".mjs",
    ".py",
    ".sh",
    ".toml",
    ".ts",
    ".txt",
    ".yaml",
    ".yml",
}
SKIP_DIRS = {
    ".git",
    ".hermes-runtime",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "inventory",
    "sessions",
    "venv",
    "node_modules",
    "dist",
    "build",
}
ARCHIVE_MARKERS = (
    "/archive/",
    "/archives/",
    "/conversations/",
    "/daily/",
    "/backups/",
    "/backup/",
)
MIGRATION_MARKERS = (
    "/migration/",
    "/notion-cutover-runtime/",
    "/recovery/",
    "/state/cutover/",
)
GENERATED_MARKERS = (
    "__pycache__",
    ".pytest_cache",
    "node_modules",
    "/dist/",
    "/build/",
    "/state/",
    ".log",
)
NOTIFICATION_KEYWORDS = ("telegram", "matrix", "photon", "imessage", "email", "webhook", "slack")
PAPERCLIP_ACTIVE_PATTERNS = (
    "paperclip_api_url",
    "paperclip_api_key",
    "paperclip_api_token",
    "paperclip_agent_id",
    "paperclip_company_id",
    "paperclipai ",
    "/api/issues",
    "paperclip-poll-wake",
    "paperclip-agent-tokens",
)


def _default_hermes_root() -> Path:
    try:
        from hermes_constants import get_default_hermes_root

        return get_default_hermes_root()
    except Exception:
        return Path.home() / ".hermes"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def redact_text(value: str) -> str:
    value = SECRET_ASSIGNMENT_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", value)
    value = ENV_SECRET_ASSIGNMENT_RE.sub(lambda m: f"{m.group(1)}<redacted>", value)
    value = KNOWN_TOKEN_RE.sub("<redacted-token>", value)
    value = FAKE_SECRET_RE.sub("<redacted-token>", value)
    return LONG_TOKEN_RE.sub("<redacted-long-token>", value)


def redact_json(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if SECRET_KEY_RE.search(str(key)):
                result[str(key)] = "<redacted>"
            else:
                result[str(key)] = redact_json(item)
        return result
    if isinstance(value, list):
        return [redact_json(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def file_sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def read_text_limited(path: Path, limit: int = 1024 * 1024) -> str | None:
    try:
        data = path.read_bytes()[:limit]
    except OSError:
        return None
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


def classify_path(path: Path) -> str:
    lowered = path.as_posix().lower()
    parts = {part.lower() for part in path.parts}
    if any(marker in lowered for marker in GENERATED_MARKERS):
        return "generated-output"
    if "site-packages" in parts or any(part.startswith("venv") for part in parts):
        return "generated-output"
    if "tests" in parts or ".test." in path.name:
        return "migration-evidence"
    if any(marker in lowered for marker in MIGRATION_MARKERS):
        return "migration-evidence"
    if any(marker in lowered for marker in ARCHIVE_MARKERS):
        return "historical-archive"
    if path.name == ".env":
        return "credential-reference"
    if path.name in ACTIVE_FILE_NAMES or path.suffix in {".py", ".sh", ".js", ".mjs", ".ts"}:
        return "active-runtime"
    if path.suffix in {".md", ".txt"}:
        return "active-documentation"
    return "unknown-review-required"


def classify_paperclip_disposition(
    path: Path,
    classification: str,
    text: str,
) -> str:
    lowered_path = path.as_posix().lower()
    lowered = text.lower()
    if classification in {"historical-archive", "migration-evidence", "generated-output"}:
        return "historical-or-migration"
    archive_implementations = (
        "/plugins/runbooks/dashboard/",
        "/scripts/export_paperclip_legacy.py",
        "/scripts/workflow_inventory.py",
        "/scripts/register_existing_runbooks.py",
        "/agent/system_prompt.py",
        "/shared-skills/paperclip-control/",
        "/runbook-migrations/",
    )
    if any(marker in lowered_path for marker in archive_implementations):
        return "read-only-archive-route"
    if any(pattern in lowered for pattern in PAPERCLIP_ACTIVE_PATTERNS) or KNOWN_TOKEN_RE.search(
        text
    ):
        return "active-execution-route"
    archive_language = (
        "archive-only",
        "archive only",
        "historical provenance",
        "historical archive",
        "not configured for this hermes profile",
        "do not create paperclip",
        "do not attempt paperclip",
        "cannot dispatch",
    )
    if any(marker in lowered for marker in archive_language):
        return "read-only-archive-route"
    return "incidental-reference"


def relative_to_any(path: Path, roots: list[Path]) -> str:
    resolved = path.resolve()
    for root in roots:
        try:
            return str(resolved.relative_to(root.resolve()))
        except ValueError:
            continue
    return str(path)


def should_scan_file(path: Path) -> bool:
    if path.name.startswith(".") and path.name != ".env":
        return False
    return path.name in ACTIVE_FILE_NAMES or path.suffix in TEXT_SUFFIXES


def iter_files(root: Path, max_files: int = 20000) -> list[Path]:
    if not root.exists():
        return []
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name
            for name in dirnames
            if name not in SKIP_DIRS
            and not name.startswith("venv")
            and not name.startswith(".tmp")
            and not name.endswith(".egg-info")
            and name not in {"worktrees", "inventory", "sessions"}
        ]
        for filename in filenames:
            path = Path(dirpath) / filename
            if should_scan_file(path):
                found.append(path)
                if len(found) >= max_files:
                    return found
    return found


def evidence_snippets(path: Path, keywords: tuple[str, ...] = KEYWORDS) -> list[str]:
    text = read_text_limited(path)
    if text is None:
        return []
    lowered = text.lower()
    snippets: list[str] = []
    for keyword in keywords:
        index = lowered.find(keyword)
        if index == -1:
            continue
        start = max(0, index - 100)
        end = min(len(text), index + len(keyword) + 160)
        snippet = " ".join(text[start:end].split())
        snippets.append(redact_text(snippet))
    return snippets[:4]


def load_json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def normalize_jobs(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        if isinstance(data.get("jobs"), list):
            return [item for item in data["jobs"] if isinstance(item, dict)]
        return [item for item in data.values() if isinstance(item, dict)]
    return []


def schedule_summary(schedule: Any) -> str:
    if isinstance(schedule, str):
        return schedule
    if isinstance(schedule, dict):
        if schedule.get("kind") == "cron":
            return str(schedule.get("expr", "cron"))
        if schedule.get("kind") == "interval":
            if "seconds" in schedule:
                return f"every {schedule.get('seconds')}s"
            if "minutes" in schedule:
                return f"every {schedule.get('minutes')}m"
        if schedule.get("kind") == "once":
            return str(schedule.get("run_at", "once"))
        return json.dumps(redact_json(schedule), sort_keys=True)
    return ""


def job_enabled(job: dict[str, Any]) -> bool:
    if "enabled" in job:
        return bool(job.get("enabled"))
    if "paused" in job:
        return not bool(job.get("paused"))
    if "status" in job:
        return str(job.get("status")).lower() not in {"paused", "disabled", "retired"}
    return True


def prompt_digest(prompt: Any) -> str | None:
    if not isinstance(prompt, str) or not prompt:
        return None
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def collect_cron_jobs(profile: Path) -> list[dict[str, Any]]:
    jobs_file = profile / "cron" / "jobs.json"
    jobs = normalize_jobs(load_json_file(jobs_file))
    records: list[dict[str, Any]] = []
    for job in jobs:
        records.append(
            {
                "id": job.get("id"),
                "name": job.get("name") or job.get("title"),
                "enabled": job_enabled(job),
                "schedule": schedule_summary(job.get("schedule") or job.get("schedule_display")),
                "schedule_raw": redact_json(job.get("schedule")),
                "deliver": redact_json(job.get("deliver")),
                "skills": redact_json(job.get("skills") or []),
                "workflow_id": job.get("workflow_id"),
                "prompt_sha256": prompt_digest(job.get("prompt")),
                "classification": "active-runtime",
                "source_path": str(jobs_file),
            }
        )
    return records


def discover_profile(profile: Path) -> dict[str, Any]:
    scripts = sorted(str(path) for path in (profile / "scripts").glob("**/*") if path.is_file())[
        :500
    ]
    plugins = sorted(str(path) for path in (profile / "plugins").glob("**/*") if path.is_file())[
        :500
    ]
    skill_files = sorted(str(path) for path in (profile / "skills").glob("**/SKILL.md"))[:500]
    runbook_files = sorted(
        str(path)
        for path in profile.glob("**/*")
        if path.is_file()
        and should_scan_file(path)
        and "runbook" in path.as_posix().lower()
        and "conversations" not in path.parts
    )[:500]
    env_file = profile / ".env"
    env_keys: list[str] = []
    if env_file.exists():
        text = read_text_limited(env_file, limit=256 * 1024) or ""
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            env_keys.append(stripped.split("=", 1)[0])
    return {
        "name": profile.name,
        "path": str(profile),
        "cron_jobs": collect_cron_jobs(profile),
        "scripts": scripts,
        "plugins": plugins,
        "skills": skill_files,
        "runbook_candidates": runbook_files,
        "env_keys": sorted(env_keys),
        "has_agents_md": (profile / "AGENTS.md").exists(),
        "has_config_yaml": (profile / "config.yaml").exists(),
    }


def collect_runbook_registry(
    hermes_root: Path,
    profiles: list[dict[str, Any]],
) -> dict[str, Any]:
    runbook_files = sorted((hermes_root / "runbooks").glob("*/RUNBOOK.md"))
    schedules = [job for profile in profiles for job in profile["cron_jobs"]]
    enabled = [job for job in schedules if job["enabled"]]
    registered = [job for job in enabled if job.get("workflow_id")]
    definitions: list[dict[str, Any]] = []
    table_counts: dict[str, int] = {}
    db_path = hermes_root / "workflow_registry.db"
    if db_path.exists():
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            definitions = [
                dict(row)
                for row in conn.execute(
                    "SELECT id, slug, name, owner_profile, status, runtime_kind, "
                    "runtime_ref, source_path, source_hash, version, updated_at "
                    "FROM workflow_definitions ORDER BY slug"
                )
            ]
            for table in (
                "workflow_definitions",
                "workflow_steps",
                "workflow_schedules",
                "workflow_runs",
                "workflow_step_runs",
            ):
                table_counts[table] = conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
            conn.close()
        except sqlite3.Error:
            definitions = []
            table_counts = {}
    candidates: list[dict[str, Any]] = []
    for path in sorted((hermes_root / "runbook-migrations").glob("*.json")):
        payload = load_json_file(path)
        if isinstance(payload, dict) and isinstance(payload.get("candidates"), list):
            candidates.extend(
                item for item in payload["candidates"] if isinstance(item, dict)
            )
    dispositions = Counter(
        str(item.get("classification") or "unclassified") for item in candidates
    )
    external = [
        item for item in definitions if item.get("runtime_kind") in {"sim", "n8n", "external_cli"}
    ]
    retained_external = [item for item in external if item.get("status") != "retired"]
    archived_external = [item for item in external if item.get("status") == "retired"]
    return {
        "runbook_files": [
            {"path": str(path), "sha256": file_sha256(path)} for path in runbook_files
        ],
        "canonical_runbook_count": len(runbook_files),
        "enabled_schedule_count": len(enabled),
        "registered_enabled_schedule_count": len(registered),
        "unregistered_enabled_schedule_count": len(enabled) - len(registered),
        "registry_db": str(db_path),
        "registry_table_counts": table_counts,
        "definitions": definitions,
        "external_runtime_definitions": external,
        "retained_external_runtime_definitions": retained_external,
        "archived_external_runtime_definitions": archived_external,
        "migration_candidate_count": len(candidates),
        "migration_dispositions": dict(dispositions),
        "migration_candidates": candidates,
    }


def run_readonly_command(args: list[str], timeout: int = 8) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "error": str(exc), "args": args}
    return {
        "available": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": redact_text(completed.stdout),
        "stderr": redact_text(completed.stderr),
        "args": args,
    }


def collect_host_schedules(skip_host_commands: bool) -> dict[str, Any]:
    if skip_host_commands:
        return {"skipped": True}
    return {
        "user_crontab": run_readonly_command(["crontab", "-l"]),
        "user_systemd_timers": run_readonly_command(
            ["systemctl", "--user", "list-timers", "--all", "--no-pager"]
        ),
        "system_systemd_timers": run_readonly_command(
            ["systemctl", "list-timers", "--all", "--no-pager"]
        ),
        "system_cron_dirs": {
            str(path): sorted(child.name for child in path.iterdir()) if path.exists() else []
            for path in (
                Path("/etc/cron.d"),
                Path("/etc/cron.daily"),
                Path("/etc/cron.hourly"),
                Path("/etc/cron.weekly"),
                Path("/etc/systemd/system"),
            )
        },
    }


def collect_paperclip_locations(paths: list[Path]) -> dict[str, Any]:
    locations: list[dict[str, Any]] = []
    sqlite_counts: list[dict[str, Any]] = []
    for root in paths:
        if not root.exists():
            locations.append({"path": str(root), "exists": False})
            continue
        files = [path for path in root.glob("**/*") if path.is_file() and "node_modules" not in path.parts]
        locations.append(
            {
                "path": str(root),
                "exists": True,
                "file_count": len(files),
                "package_files": sorted(str(path) for path in files if path.name == "package.json"),
                "sqlite_files": sorted(str(path) for path in files if path.suffix in {".db", ".sqlite", ".sqlite3"}),
            }
        )
        for db_path in [path for path in files if path.suffix in {".db", ".sqlite", ".sqlite3"}]:
            sqlite_counts.append(sqlite_table_counts(db_path))
    return {"locations": locations, "sqlite_counts": sqlite_counts}


def sqlite_table_counts(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"path": str(path), "tables": {}, "error": None}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            for (table,) in tables:
                safe_table = str(table).replace('"', '""')
                count = conn.execute(f'SELECT COUNT(*) FROM "{safe_table}"').fetchone()[0]
                result["tables"][table] = count
        finally:
            conn.close()
    except sqlite3.Error as exc:
        result["error"] = str(exc)
    return result


def scan_references(scan_roots: list[Path]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for root in scan_roots:
        for path in iter_files(root):
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            if resolved in seen:
                continue
            seen.add(resolved)
            snippets = evidence_snippets(path)
            if not snippets:
                continue
            classification = classify_path(path)
            text = read_text_limited(path) or ""
            evidence.append(
                {
                    "path": str(path),
                    "classification": classification,
                    "sha256": file_sha256(path),
                    "keywords": sorted(
                        keyword
                        for keyword in KEYWORDS
                        if keyword in " ".join(snippets).lower() or keyword in path.as_posix().lower()
                    ),
                    "snippets": snippets,
                    "paperclip_disposition": classify_paperclip_disposition(
                        path, classification, text
                    )
                    if "paperclip" in text.lower()
                    or "paperclip" in path.as_posix().lower()
                    else None,
                }
            )
    return evidence


def build_schedule_collision_report(profiles: list[dict[str, Any]]) -> dict[str, Any]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for profile in profiles:
        for job in profile["cron_jobs"]:
            if not job["enabled"]:
                continue
            key = "|".join(
                [
                    str(job.get("schedule") or ""),
                    str(job.get("name") or ""),
                    str(job.get("deliver") or ""),
                ]
            )
            buckets[key].append(
                {
                    "profile": profile["name"],
                    "job_id": job.get("id"),
                    "name": job.get("name"),
                    "schedule": job.get("schedule"),
                    "deliver": job.get("deliver"),
                }
            )
    collisions = [
        {"collision_key": key, "sources": values}
        for key, values in sorted(buckets.items())
        if key.strip("|") and len(values) > 1
    ]
    return {"collision_count": len(collisions), "collisions": collisions}


def build_notification_inventory(profiles: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    paths: list[dict[str, Any]] = []
    for profile in profiles:
        for job in profile["cron_jobs"]:
            haystack = " ".join(
                str(job.get(key) or "") for key in ("deliver", "name", "schedule", "skills")
            ).lower()
            hits = [keyword for keyword in NOTIFICATION_KEYWORDS if keyword in haystack]
            if hits:
                paths.append(
                    {
                        "profile": profile["name"],
                        "source": "cron",
                        "job_id": job.get("id"),
                        "name": job.get("name"),
                        "channels": hits,
                    }
                )
    for item in evidence:
        hits = [keyword for keyword in NOTIFICATION_KEYWORDS if keyword in item.get("keywords", [])]
        if hits:
            paths.append(
                {
                    "source": "file-reference",
                    "path": item["path"],
                    "classification": item["classification"],
                    "channels": hits,
                }
            )
    return {"notification_path_count": len(paths), "paths": paths}


def build_authority_map(profiles: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for profile in profiles:
        for job in profile["cron_jobs"]:
            rows.append(
                {
                    "owner": profile["name"],
                    "runtime": "Hermes Cron",
                    "source": f"{profile['path']}/cron/jobs.json#{job.get('id')}",
                    "enabled": job["enabled"],
                    "disposition": "retain" if job["enabled"] else "investigate",
                    "notes": "Unregistered schedule until workflow_id is populated"
                    if not job.get("workflow_id")
                    else f"workflow_id={job.get('workflow_id')}",
                }
            )
    for item in evidence:
        if "paperclip" not in item.get("keywords", []):
            continue
        rows.append(
            {
                "owner": "unknown-review-required",
                "runtime": "Paperclip reference",
                "source": item["path"],
                "enabled": item.get("paperclip_disposition")
                == "active-execution-route",
                "disposition": "investigate"
                if item.get("paperclip_disposition") == "active-execution-route"
                else "archive",
                "notes": item.get("paperclip_disposition") or item["classification"],
            }
        )
    return rows


def collect_paperclip_export_reconciliation(hermes_root: Path) -> dict[str, Any]:
    path = hermes_root / "archives" / "paperclip" / "current" / "reconciliation.json"
    payload = load_json_file(path)
    if isinstance(payload, dict):
        missing = payload.get("foreign_key_missing_counts", {})
        return {
            **payload,
            "status": "reconciled"
            if not payload.get("count_mismatches")
            and not any(int(value) for value in missing.values())
            else "failed",
            "archive_reconciliation": str(path.resolve()),
        }
    return {
        "status": "metadata-only",
        "source_counts": [],
        "export_counts": None,
        "note": "Full Paperclip export reconciliation is produced by the archive/export step.",
    }


def markdown_inventory(inventory: dict[str, Any]) -> str:
    profiles = inventory["profiles"]
    counts = inventory["counts"]
    lines = [
        "# Workflow Inventory",
        "",
        f"Generated: {inventory['generated_at']}",
        f"Hermes root: `{inventory['hermes_root']}`",
        "",
        "## Counts",
        "",
    ]
    for key in sorted(counts):
        lines.append(f"- `{key}`: {counts[key]}")
    lines.extend(["", "## Profiles", ""])
    for profile in profiles:
        enabled = sum(1 for job in profile["cron_jobs"] if job["enabled"])
        lines.append(
            f"- `{profile['name']}`: {len(profile['cron_jobs'])} cron jobs "
            f"({enabled} enabled), {len(profile['skills'])} skills, "
            f"{len(profile['scripts'])} scripts"
        )
    lines.extend(["", "## Active Paperclip References", ""])
    active_paperclip = inventory["paperclip_active_dependencies"]["active_dependencies"]
    if not active_paperclip:
        lines.append("- None found in scanned active runtime surfaces.")
    else:
        for item in active_paperclip[:200]:
            lines.append(f"- `{item['classification']}` `{item['path']}`")
    lines.extend(["", "## Schedule Collisions", ""])
    collisions = inventory["schedule_collision_report"]["collisions"]
    if not collisions:
        lines.append("- None found among enabled Hermes Cron jobs.")
    else:
        for collision in collisions[:100]:
            lines.append(f"- `{collision['collision_key']}`: {len(collision['sources'])} sources")
    lines.append("")
    return "\n".join(lines)


def markdown_authority_map(rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Active Automation Authority Map",
        "",
        "| Owner | Runtime | Enabled | Disposition | Source | Notes |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            "| {owner} | {runtime} | {enabled} | {disposition} | `{source}` | {notes} |".format(
                owner=str(row["owner"]).replace("|", "\\|"),
                runtime=str(row["runtime"]).replace("|", "\\|"),
                enabled="yes" if row["enabled"] else "no",
                disposition=str(row["disposition"]).replace("|", "\\|"),
                source=str(row["source"]).replace("|", "\\|"),
                notes=str(row["notes"]).replace("|", "\\|"),
            )
        )
    lines.append("")
    return "\n".join(lines)


@dataclass
class InventoryOptions:
    hermes_root: Path
    scan_roots: list[Path]
    paperclip_roots: list[Path]
    output_dir: Path
    skip_host_commands: bool = False


def build_inventory(options: InventoryOptions) -> dict[str, Any]:
    profiles_root = options.hermes_root / "profiles"
    profiles = [
        discover_profile(path)
        for path in sorted(profiles_root.iterdir() if profiles_root.exists() else [])
        if path.is_dir()
    ]
    evidence = scan_references(options.scan_roots)
    paperclip = collect_paperclip_locations(options.paperclip_roots)
    host_schedules = collect_host_schedules(options.skip_host_commands)
    schedule_collision_report = build_schedule_collision_report(profiles)
    notification_inventory = build_notification_inventory(profiles, evidence)
    authority_rows = build_authority_map(profiles, evidence)
    runbook_registry = collect_runbook_registry(options.hermes_root, profiles)
    active_paperclip = [
        item
        for item in evidence
        if "paperclip" in item.get("keywords", [])
        and item.get("paperclip_disposition") == "active-execution-route"
    ]
    counts = Counter()
    counts["profiles"] = len(profiles)
    counts["cron_jobs"] = sum(len(profile["cron_jobs"]) for profile in profiles)
    counts["enabled_cron_jobs"] = sum(
        1 for profile in profiles for job in profile["cron_jobs"] if job["enabled"]
    )
    counts["evidence_items"] = len(evidence)
    counts["active_paperclip_dependencies"] = len(active_paperclip)
    counts["schedule_collisions"] = schedule_collision_report["collision_count"]
    counts["canonical_runbooks"] = runbook_registry["canonical_runbook_count"]
    counts["registered_enabled_cron_jobs"] = runbook_registry[
        "registered_enabled_schedule_count"
    ]
    counts["unregistered_enabled_cron_jobs"] = runbook_registry[
        "unregistered_enabled_schedule_count"
    ]
    counts["runbook_migration_candidates"] = runbook_registry[
        "migration_candidate_count"
    ]
    counts["retained_external_runtime_workflows"] = len(
        runbook_registry["retained_external_runtime_definitions"]
    )
    counts["archived_external_runtime_workflows"] = len(
        runbook_registry["archived_external_runtime_definitions"]
    )

    inventory = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "hermes_root": str(options.hermes_root),
        "scan_roots": [str(path) for path in options.scan_roots],
        "profiles": profiles,
        "evidence": evidence,
        "host_schedules": host_schedules,
        "paperclip": paperclip,
        "paperclip_active_dependencies": {
            "active_dependency_count": len(active_paperclip),
            "active_dependencies": active_paperclip,
        },
        "schedule_collision_report": schedule_collision_report,
        "notification_path_inventory": notification_inventory,
        "runbook_registry": runbook_registry,
        "active_automation_authority_map": authority_rows,
        "paperclip_export_reconciliation": collect_paperclip_export_reconciliation(
            options.hermes_root
        ),
        "counts": dict(counts),
    }
    return redact_json(inventory)


def write_reports(inventory: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    reports = {
        "workflow-inventory.json": inventory,
        "paperclip-active-dependencies.json": inventory["paperclip_active_dependencies"],
        "schedule-collision-report.json": inventory["schedule_collision_report"],
        "paperclip-export-reconciliation.json": inventory["paperclip_export_reconciliation"],
        "notification-path-inventory.json": inventory["notification_path_inventory"],
    }
    for name, data in reports.items():
        (output_dir / name).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output_dir / "workflow-inventory.md").write_text(markdown_inventory(inventory), encoding="utf-8")
    (output_dir / "active-automation-authority-map.md").write_text(
        markdown_authority_map(inventory["active_automation_authority_map"]),
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-root", type=Path, default=_default_hermes_root())
    parser.add_argument(
        "--scan-root",
        action="append",
        type=Path,
        dest="scan_roots",
        help="Root to scan for active automation references. May be repeated.",
    )
    parser.add_argument(
        "--paperclip-root",
        action="append",
        type=Path,
        dest="paperclip_roots",
        help="Paperclip installation/config root. May be repeated.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path.cwd() / ".hermes" / "inventory",
        help="Directory for generated inventory reports.",
    )
    parser.add_argument(
        "--skip-host-commands",
        action="store_true",
        help="Do not run read-only crontab/systemctl inventory commands.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    hermes_root = args.hermes_root.expanduser().resolve()
    scan_roots = [path.expanduser().resolve() for path in (args.scan_roots or [hermes_root, REPO_ROOT])]
    paperclip_roots = [
        path.expanduser().resolve()
        for path in (
            args.paperclip_roots
            or [Path.home() / ".paperclip", Path.home() / ".local" / "opt" / "paperclip"]
        )
    ]
    options = InventoryOptions(
        hermes_root=hermes_root,
        scan_roots=scan_roots,
        paperclip_roots=paperclip_roots,
        output_dir=args.output_dir.expanduser().resolve(),
        skip_host_commands=args.skip_host_commands,
    )
    inventory = build_inventory(options)
    write_reports(inventory, options.output_dir)
    print(f"Wrote {len(DEFAULT_REPORT_NAMES)} reports to {options.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
