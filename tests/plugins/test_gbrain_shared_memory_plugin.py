from __future__ import annotations

import json
from pathlib import Path

from plugins.gbrain_shared_memory import (
    _build_window,
    _format_context,
    _format_facts,
    on_post_llm_call,
    on_pre_llm_call,
)
from plugins.gbrain_shared_memory.queue import enqueue_turn


def test_build_window_uses_recent_bounded_turns():
    history = [
        {"role": "user", "content": "old " * 500},
        {"role": "assistant", "content": "middle"},
    ]

    window = _build_window(history, "newest")

    assert "newest" in window
    assert len(window) <= 5000
    assert window.splitlines()[-1] == "user: newest"


def test_format_context_is_source_labelled_and_bounded():
    result = {
        "pages": [
            {
                "source_id": "shared",
                "display": "Elliott",
                "slug": "people/elliott",
                "confidence": 0.95,
                "rationale": "alias match",
                "synopsis": "Prefers concise decision summaries.",
            }
        ]
    }

    context = _format_context(result)

    assert "[shared]" in context
    assert "people/elliott" in context
    assert "verify current operational truth in Craft" in context


def test_format_facts_ranks_relevant_shared_memory():
    result = {
        "facts": [
            {
                "id": 11,
                "fact": "Elliott prefers morning workouts.",
                "confidence": 0.95,
                "notability": "high",
            },
            {
                "id": 12,
                "fact": "Elliott uses a blue notebook.",
                "confidence": 0.8,
                "notability": "low",
            },
        ]
    }

    context = _format_facts(result, "When does Elliott prefer to work out?")

    assert "morning workouts" in context
    assert "GBrain shared facts" in context
    assert "blue notebook" not in context


def test_pre_hook_injects_context(monkeypatch):
    def fake_call(tool, *args, **kwargs):
        if tool == "recall":
            return {"facts": []}
        return {
            "pages": [
                {
                    "source_id": "shared",
                    "display": "Elliott",
                    "slug": "people/elliott",
                    "confidence": 0.95,
                    "rationale": "alias match",
                    "synopsis": "Uses Craft for reference records.",
                }
            ]
        }

    monkeypatch.setattr("plugins.gbrain_shared_memory._call_gbrain", fake_call)

    result = on_pre_llm_call(
        session_id="session-1",
        user_message="What does Elliott use for records?",
        conversation_history=[],
        platform="telegram",
    )

    assert result and "context" in result
    assert "Uses Craft" in result["context"]


def test_pre_hook_injects_shared_facts(monkeypatch):
    def fake_call(tool, *args, **kwargs):
        if tool == "recall":
            return {
                "facts": [
                    {
                        "id": 31,
                        "fact": "Elliott prefers concise decision summaries.",
                        "confidence": 0.9,
                        "notability": "high",
                    }
                ]
            }
        return {"pages": []}

    monkeypatch.setattr("plugins.gbrain_shared_memory._call_gbrain", fake_call)

    result = on_pre_llm_call(
        session_id="session-2",
        user_message="How should I summarize this decision for Elliott?",
    )

    assert result and "concise decision summaries" in result["context"]


def test_pre_hook_fails_open(monkeypatch):
    def fail(*args, **kwargs):
        raise TimeoutError("bounded test timeout")

    monkeypatch.setattr("plugins.gbrain_shared_memory._call_gbrain", fail)

    assert on_pre_llm_call(session_id="s", user_message="hello") is None


def test_enqueue_is_idempotent_private_and_profile_scoped(tmp_path):
    first = enqueue_turn(
        home=tmp_path,
        profile="alina",
        session_id="s1",
        turn_id="t1",
        user_message="Elliott prefers concise summaries.",
        platform="telegram",
    )
    second = enqueue_turn(
        home=tmp_path,
        profile="alina",
        session_id="s1",
        turn_id="t1",
        user_message="Elliott prefers concise summaries.",
        platform="telegram",
    )

    assert first == second
    assert len(list((tmp_path / "state" / "gbrain-memory" / "outbox").glob("*.json"))) == 1
    assert first.stat().st_mode & 0o777 == 0o600
    payload = json.loads(first.read_text())
    assert payload["profile"] == "alina"
    assert payload["idempotency_key"] == first.stem


def test_post_hook_queues_without_assistant_text(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PROFILE", "grace")

    on_post_llm_call(
        session_id="s2",
        turn_id="t2",
        user_message="I prefer morning workouts.",
        assistant_response="private assistant output must not be queued",
        platform="matrix",
    )

    queued = next((tmp_path / "state" / "gbrain-memory" / "outbox").glob("*.json"))
    raw = queued.read_text()
    assert "I prefer morning workouts" in raw
    assert "private assistant output" not in raw
