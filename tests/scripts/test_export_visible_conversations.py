from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "export_visible_conversations.py"


def _load_exporter():
    assert SCRIPT.exists(), "visible conversation exporter has not been implemented"
    spec = importlib.util.spec_from_file_location("export_visible_conversations", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def profile(tmp_path: Path) -> Path:
    root = tmp_path / "profiles" / "kenzie"
    root.mkdir(parents=True)
    conn = sqlite3.connect(root / "state.db")
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            chat_type TEXT,
            thread_id TEXT,
            display_name TEXT,
            started_at REAL NOT NULL,
            title TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            tool_call_id TEXT,
            tool_calls TEXT,
            tool_name TEXT,
            timestamp REAL NOT NULL,
            reasoning TEXT,
            reasoning_content TEXT,
            reasoning_details TEXT,
            codex_reasoning_items TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            compacted INTEGER NOT NULL DEFAULT 0,
            observed INTEGER DEFAULT 0
        );
        """
    )
    conn.commit()
    conn.close()
    return root


def _timestamp(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _session(profile: Path, sid: str, source: str = "telegram", **values) -> None:
    defaults = {
        "chat_type": "dm",
        "thread_id": None,
        "display_name": None,
        "started_at": _timestamp("2026-07-17T12:00:00Z"),
        "title": None,
    }
    defaults.update(values)
    with sqlite3.connect(profile / "state.db") as conn:
        conn.execute(
            "INSERT INTO sessions "
            "(id, source, chat_type, thread_id, display_name, started_at, title) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                sid,
                source,
                defaults["chat_type"],
                defaults["thread_id"],
                defaults["display_name"],
                defaults["started_at"],
                defaults["title"],
            ),
        )


def _message(
    profile: Path,
    sid: str,
    role: str,
    content,
    timestamp: float,
    **values,
) -> None:
    columns = [
        "session_id",
        "role",
        "content",
        "timestamp",
        "tool_call_id",
        "tool_calls",
        "tool_name",
        "reasoning",
        "reasoning_content",
        "reasoning_details",
        "codex_reasoning_items",
        "active",
        "compacted",
        "observed",
    ]
    row = {
        "session_id": sid,
        "role": role,
        "content": content,
        "timestamp": timestamp,
        "tool_call_id": None,
        "tool_calls": None,
        "tool_name": None,
        "reasoning": None,
        "reasoning_content": None,
        "reasoning_details": None,
        "codex_reasoning_items": None,
        "active": 1,
        "compacted": 0,
        "observed": 0,
    }
    row.update(values)
    with sqlite3.connect(profile / "state.db") as conn:
        conn.execute(
            f"INSERT INTO messages ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            [row[column] for column in columns],
        )


def test_exports_only_visible_addressed_conversation(profile: Path) -> None:
    exporter = _load_exporter()
    ts = _timestamp("2026-07-17T15:00:00Z")
    _session(profile, "chat")
    _message(profile, "chat", "user", "Visible question", ts)
    _message(
        profile,
        "chat",
        "assistant",
        "Internal progress text",
        ts + 1,
        tool_calls=json.dumps([{"name": "terminal"}]),
        reasoning="hidden reasoning",
    )
    _message(profile, "chat", "tool", "tool output", ts + 2, tool_call_id="call-1")
    _message(
        profile,
        "chat",
        "assistant",
        "Visible answer",
        ts + 3,
        reasoning="private chain of thought",
        reasoning_content="private reasoning content",
    )
    _message(profile, "chat", "system", "system prompt", ts + 4)
    _message(profile, "chat", "session_meta", "tool definitions", ts + 5)
    _message(profile, "chat", "user", "Observed group chatter", ts + 6, observed=1)
    _message(profile, "chat", "assistant", "   ", ts + 7)

    for source in ("cron", "subagent", "tool", "vt-monitor"):
        sid = f"internal-{source}"
        _session(profile, sid, source=source)
        _message(profile, sid, "user", f"{source} request", ts)
        _message(profile, sid, "assistant", f"{source} response", ts + 1)

    result = exporter.export_profile(profile)
    output = profile / "conversations" / "daily" / "2026-07-17.md"
    text = output.read_text(encoding="utf-8")

    assert result.messages == 2
    assert "Visible question" in text
    assert "Visible answer" in text
    for hidden in (
        "Internal progress text",
        "tool output",
        "system prompt",
        "tool definitions",
        "Observed group chatter",
        "private chain of thought",
        "private reasoning content",
        "cron request",
        "subagent response",
        "vt-monitor response",
    ):
        assert hidden not in text


def test_recovers_compacted_originals_without_summaries_or_duplicates(profile: Path) -> None:
    exporter = _load_exporter()
    ts = _timestamp("2026-07-17T16:00:00Z")
    _session(profile, "chat")
    _message(profile, "chat", "user", "Original question", ts, active=0, compacted=1)
    _message(profile, "chat", "assistant", "Original answer", ts + 1, active=0, compacted=1)
    _message(profile, "chat", "user", "Original question", ts, active=1, compacted=0)
    _message(profile, "chat", "assistant", "Original answer", ts + 1, active=1, compacted=0)
    _message(
        profile,
        "chat",
        "assistant",
        "Synthetic summary\n\n--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---",
        ts + 2,
    )
    _message(profile, "chat", "user", "New question", ts + 3)
    _message(profile, "chat", "assistant", "New answer", ts + 4)

    result = exporter.export_profile(profile)
    text = (profile / "conversations" / "daily" / "2026-07-17.md").read_text()

    assert result.messages == 4
    assert text.count("Original question") == 1
    assert text.count("Original answer") == 1
    assert "Synthetic summary" not in text
    assert "New question" in text
    assert "New answer" in text


def test_structured_content_is_readable_and_secrets_are_redacted(profile: Path) -> None:
    exporter = _load_exporter()
    ts = _timestamp("2026-07-17T17:00:00Z")
    _session(profile, "chat", source="bluebubbles")
    structured = "\x00json:" + json.dumps(
        [
            {"type": "text", "text": "Here is the photo"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]
    )
    fake_token = "123456789:" + "A" * 36
    _message(profile, "chat", "user", structured, ts)
    _message(profile, "chat", "assistant", f"Credential was {fake_token}", ts + 1)
    _message(
        profile,
        "chat",
        "user",
        "Legacy screenshot data:image/png;base64," + "A" * 80,
        ts + 2,
    )

    exporter.export_profile(profile)
    text = (profile / "conversations" / "daily" / "2026-07-17.md").read_text()

    assert "Here is the photo" in text
    assert "[Attachment: image]" in text
    assert "data:image" not in text
    assert fake_token not in text
    assert "123456789:***" in text


def test_uses_local_dates_and_keeps_unknown_timestamps_out_of_1970(profile: Path) -> None:
    exporter = _load_exporter()
    _session(profile, "chat")
    _message(profile, "chat", "user", "Late UTC message", _timestamp("2026-07-18T02:30:00Z"))
    _message(profile, "chat", "assistant", "Timestamp unavailable", 0)

    exporter.export_profile(profile, timezone_name="America/Chicago")

    assert (profile / "conversations" / "daily" / "2026-07-17.md").exists()
    assert (profile / "conversations" / "daily" / "undated.md").exists()
    assert not (profile / "conversations" / "daily" / "1970-01-01.md").exists()


def test_rerun_is_idempotent_and_files_are_owner_only(profile: Path) -> None:
    exporter = _load_exporter()
    ts = _timestamp("2026-07-17T18:00:00Z")
    _session(profile, "chat")
    _message(profile, "chat", "user", "Question", ts)
    _message(profile, "chat", "assistant", "Answer", ts + 1)

    first = exporter.export_profile(profile)
    output = profile / "conversations" / "daily" / "2026-07-17.md"
    first_stat = output.stat()
    second = exporter.export_profile(profile)
    second_stat = output.stat()

    assert first.written_files == 1
    assert second.written_files == 0
    assert second.unchanged_files == 1
    assert first_stat.st_mtime_ns == second_stat.st_mtime_ns
    assert os.stat(output).st_mode & 0o777 == 0o600
    assert os.stat(output.parent).st_mode & 0o777 == 0o700


def test_dry_run_does_not_create_archive(profile: Path) -> None:
    exporter = _load_exporter()
    ts = _timestamp("2026-07-17T18:00:00Z")
    _session(profile, "chat")
    _message(profile, "chat", "user", "Question", ts)

    result = exporter.export_profile(profile, dry_run=True)

    assert result.messages == 1
    assert result.planned_files == 1
    assert not (profile / "conversations").exists()


def test_main_can_stage_archives_under_a_separate_output_root(
    profile: Path, tmp_path: Path
) -> None:
    exporter = _load_exporter()
    ts = _timestamp("2026-07-17T18:00:00Z")
    _session(profile, "chat")
    _message(profile, "chat", "user", "Question", ts)
    output_root = tmp_path / "staged"

    result = exporter.main(
        [
            "--profiles-root",
            str(profile.parent),
            "--profile",
            profile.name,
            "--output-root",
            str(output_root),
        ]
    )

    assert result == 0
    assert (
        output_root
        / profile.name
        / "conversations"
        / "daily"
        / "2026-07-17.md"
    ).exists()
    assert not (profile / "conversations").exists()
