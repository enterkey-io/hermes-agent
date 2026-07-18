#!/usr/bin/env python3
"""Export human-visible Hermes conversations into profile-local daily Markdown."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.redact import redact_sensitive_text  # noqa: E402


DEFAULT_PROFILES_ROOT = Path.home() / ".hermes" / "profiles"
DEFAULT_TIMEZONE = "America/Chicago"
DEFAULT_EXCLUDED_SOURCES = frozenset(
    {
        "cron",
        "subagent",
        "tool",
        "vt-monitor",
        "catalog",
        "api_server",
    }
)
CONTENT_JSON_PREFIX = "\x00json:"
SUMMARY_MARKERS = (
    "[CONTEXT COMPACTION — REFERENCE ONLY]",
    "[CONTEXT SUMMARY]:",
    "[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]",
    "--- END OF CONTEXT SUMMARY —",
)
ATTACHMENT_TYPES = {
    "image": "image",
    "image_url": "image",
    "input_image": "image",
    "audio": "audio",
    "input_audio": "audio",
    "file": "file",
    "document": "file",
}
DATA_URI_RE = re.compile(
    r"data:(image|audio|application)/[A-Za-z0-9.+-]+(?:;[^,\s]+)*;base64,[A-Za-z0-9+/=_-]+",
    re.IGNORECASE,
)


class ExportStats(NamedTuple):
    profile: str
    messages: int
    planned_files: int
    written_files: int
    unchanged_files: int


class VisibleMessage(NamedTuple):
    row_id: int
    session_id: str
    source: str
    chat_type: str
    thread_id: str
    display_name: str
    role: str
    text: str
    timestamp: float | None
    compacted: bool


def _decode_content(content: Any) -> Any:
    if isinstance(content, str) and content.startswith(CONTENT_JSON_PREFIX):
        try:
            return json.loads(content[len(CONTENT_JSON_PREFIX) :])
        except (json.JSONDecodeError, TypeError):
            return content
    return content


def _structured_text(content: Any) -> str:
    content = _decode_content(content)
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (int, float)):
        return str(content)
    if isinstance(content, list):
        parts = [_structured_text(part).strip() for part in content]
        return "\n\n".join(part for part in parts if part)
    if isinstance(content, dict):
        part_type = str(content.get("type") or "").strip().lower()
        text_value = content.get("text")
        parts: list[str] = []
        if isinstance(text_value, str) and text_value.strip():
            parts.append(text_value.strip())
        attachment = ATTACHMENT_TYPES.get(part_type)
        if attachment:
            parts.append(f"[Attachment: {attachment}]")
        return "\n\n".join(parts)
    return str(content)


def _normalize_text(content: Any) -> str:
    text = _structured_text(content).replace("\x00", "").strip()
    if not text:
        return ""
    text = DATA_URI_RE.sub(
        lambda match: (
            "[Attachment: image]"
            if match.group(1).lower() == "image"
            else "[Attachment: audio]"
            if match.group(1).lower() == "audio"
            else "[Attachment: file]"
        ),
        text,
    )
    return redact_sensitive_text(text, force=True).strip()


def _is_summary(text: str) -> bool:
    return any(marker in text for marker in SUMMARY_MARKERS)


def _read_rows(profile_dir: Path) -> list[sqlite3.Row]:
    db_path = (profile_dir / "state.db").resolve()
    if not db_path.is_file():
        raise FileNotFoundError(f"state database not found for profile {profile_dir.name}")
    connection = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        placeholders = ",".join("?" for _ in DEFAULT_EXCLUDED_SOURCES)
        return connection.execute(
            f"""
            SELECT
                m.id AS row_id,
                m.session_id,
                s.source,
                COALESCE(s.chat_type, '') AS chat_type,
                COALESCE(s.thread_id, '') AS thread_id,
                COALESCE(s.display_name, '') AS display_name,
                m.role,
                m.content,
                m.timestamp,
                COALESCE(m.active, 1) AS active,
                COALESCE(m.compacted, 0) AS compacted,
                COALESCE(m.observed, 0) AS observed,
                m.tool_calls
            FROM messages AS m
            JOIN sessions AS s ON s.id = m.session_id
            WHERE m.role IN ('user', 'assistant')
              AND (COALESCE(m.active, 1) = 1 OR COALESCE(m.compacted, 0) = 1)
              AND COALESCE(m.observed, 0) = 0
              AND (m.role != 'assistant' OR m.tool_calls IS NULL)
              AND s.source NOT IN ({placeholders})
            ORDER BY m.timestamp, m.id
            """,
            sorted(DEFAULT_EXCLUDED_SOURCES),
        ).fetchall()
    finally:
        connection.close()


def _visible_messages(profile_dir: Path) -> list[VisibleMessage]:
    messages: list[VisibleMessage] = []
    seen: set[tuple[str, str, float | None, str]] = set()
    for row in _read_rows(profile_dir):
        text = _normalize_text(row["content"])
        if not text or _is_summary(text):
            continue
        raw_timestamp = row["timestamp"]
        try:
            timestamp = float(raw_timestamp)
        except (TypeError, ValueError):
            timestamp = None
        if timestamp is not None and timestamp <= 0:
            timestamp = None
        dedupe_key = (row["session_id"], row["role"], timestamp, text)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        messages.append(
            VisibleMessage(
                row_id=int(row["row_id"]),
                session_id=str(row["session_id"]),
                source=str(row["source"]),
                chat_type=str(row["chat_type"]),
                thread_id=str(row["thread_id"]),
                display_name=str(row["display_name"]),
                role=str(row["role"]),
                text=text,
                timestamp=timestamp,
                compacted=bool(row["compacted"]),
            )
        )
    return messages


def _date_key(timestamp: float | None, tz: ZoneInfo) -> str:
    if timestamp is None:
        return "undated"
    return datetime.fromtimestamp(timestamp, tz=tz).date().isoformat()


def _time_label(timestamp: float | None, tz: ZoneInfo) -> str:
    if timestamp is None:
        return "Time unavailable"
    local = datetime.fromtimestamp(timestamp, tz=tz)
    hour = local.hour % 12 or 12
    return f"{hour}:{local.minute:02d} {local.strftime('%p')}"


def _source_label(message: VisibleMessage) -> str:
    known = {
        "bluebubbles": "BlueBubbles",
        "nanoclaw-archive": "NanoClaw archive",
        "photon": "VOX/Photon",
        "telegram": "Telegram",
        "matrix": "Matrix",
        "voice": "Voice",
        "cli": "CLI",
    }
    source = known.get(message.source, message.source.replace("_", " ").title())
    details: list[str] = []
    if message.chat_type:
        details.append(message.chat_type.upper() if message.chat_type == "dm" else message.chat_type.title())
    if message.display_name:
        details.append(_normalize_text(message.display_name))
    if message.thread_id:
        details.append(f"topic {message.thread_id}")
    return " - ".join([source, *[detail for detail in details if detail]])


def _render_day(
    profile_name: str,
    date_key: str,
    messages: Iterable[VisibleMessage],
    tz: ZoneInfo,
    user_name: str,
) -> str:
    display_profile = profile_name.replace("-", " ").title()
    if date_key == "undated":
        title_date = "Undated"
    else:
        date_value = datetime.strptime(date_key, "%Y-%m-%d")
        title_date = f"{date_value.strftime('%B')} {date_value.day}, {date_value.year}"
    lines = [
        "---",
        f'profile: "{profile_name}"',
        f'date: "{date_key}"',
        f'timezone: "{tz.key}"',
        'archive_type: "visible-conversation"',
        "---",
        "",
        f"# {display_profile} conversations - {title_date}",
        "",
    ]
    current_session: str | None = None
    for message in messages:
        if message.session_id != current_session:
            if current_session is not None:
                lines.append("")
            lines.extend(
                [
                    f"## {_source_label(message)}",
                    "",
                    f"<!-- session_id: {message.session_id} -->",
                    "",
                ]
            )
            current_session = message.session_id
        speaker = user_name if message.role == "user" else display_profile
        lines.extend(
            [
                f"**{_time_label(message.timestamp, tz)} - {speaker}**",
                "",
                message.text,
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _prepare_output_dir(path: Path) -> None:
    if path.exists() and path.is_symlink():
        raise RuntimeError(f"refusing symlinked output directory: {path}")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.chmod(0o700)


def _write_if_changed(path: Path, content: str) -> bool:
    if path.exists():
        if path.is_symlink():
            raise RuntimeError(f"refusing symlinked output file: {path}")
        if path.read_text(encoding="utf-8") == content:
            path.chmod(0o600)
            return False
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        path.chmod(0o600)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return True


def export_profile(
    profile_dir: Path | str,
    *,
    output_dir: Path | str | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    user_name: str = "Elliott",
    dates: set[str] | None = None,
    dry_run: bool = False,
) -> ExportStats:
    profile_path = Path(profile_dir).expanduser().resolve()
    try:
        tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown timezone: {timezone_name}") from exc
    messages = _visible_messages(profile_path)
    grouped: dict[str, list[VisibleMessage]] = defaultdict(list)
    for message in messages:
        key = _date_key(message.timestamp, tz)
        if dates is None or key in dates:
            grouped[key].append(message)

    rendered = {
        key: _render_day(profile_path.name, key, day_messages, tz, user_name)
        for key, day_messages in sorted(grouped.items())
    }
    if dry_run:
        return ExportStats(profile_path.name, sum(len(v) for v in grouped.values()), len(rendered), 0, 0)

    archive_dir = (
        Path(output_dir).expanduser().resolve()
        if output_dir is not None
        else profile_path / "conversations" / "daily"
    )
    _prepare_output_dir(archive_dir)
    written = 0
    unchanged = 0
    for key, content in rendered.items():
        filename = "undated.md" if key == "undated" else f"{key}.md"
        if _write_if_changed(archive_dir / filename, content):
            written += 1
        else:
            unchanged += 1
    return ExportStats(
        profile_path.name,
        sum(len(v) for v in grouped.values()),
        len(rendered),
        written,
        unchanged,
    )


def _profiles(root: Path, selected: list[str] | None) -> list[Path]:
    if selected:
        profiles = [root / name for name in selected]
    else:
        profiles = [path.parent for path in root.glob("*/state.db")]
    return sorted(profiles, key=lambda path: path.name.lower())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export human-visible Hermes conversations to daily Markdown."
    )
    parser.add_argument("--profiles-root", type=Path, default=DEFAULT_PROFILES_ROOT)
    parser.add_argument("--profile", action="append", dest="profiles")
    parser.add_argument(
        "--output-root",
        type=Path,
        help="Stage as OUTPUT_ROOT/<profile>/conversations/daily instead of writing profiles",
    )
    parser.add_argument("--date", action="append", dest="dates", help="YYYY-MM-DD or undated")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument("--user-name", default="Elliott")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.profiles_root.expanduser().resolve()
    output_root = args.output_root.expanduser().resolve() if args.output_root else None
    failures = 0
    profiles = _profiles(root, args.profiles)
    if not profiles:
        print(f"No Hermes profiles with state.db found under {root}", file=sys.stderr)
        return 1
    for profile in profiles:
        try:
            stats = export_profile(
                profile,
                output_dir=(
                    output_root / profile.name / "conversations" / "daily"
                    if output_root is not None
                    else None
                ),
                timezone_name=args.timezone,
                user_name=args.user_name,
                dates=set(args.dates) if args.dates else None,
                dry_run=args.dry_run,
            )
            print(
                f"profile={stats.profile} messages={stats.messages} "
                f"files={stats.planned_files} written={stats.written_files} "
                f"unchanged={stats.unchanged_files} dry_run={str(args.dry_run).lower()}"
            )
        except Exception as exc:
            failures += 1
            print(f"profile={profile.name} error={type(exc).__name__}: {exc}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
