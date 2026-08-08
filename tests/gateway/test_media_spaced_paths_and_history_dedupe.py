"""Regression tests: spaced paths, GIS extensions, cross-turn dedupe plumbing,
and code-block-safe streaming display strip.

Covers the follow-up wave after PR #72170:

* #24032 — GIS extensions (.kmz/.kml/.geojson/.gpx) are deliverable, and
  unknown-extension paths containing spaces extract via the
  validation-gated progressive right-trim (``_spaced_path_candidates``).
* #16434 half — ``strip_media_directives_for_display`` (streaming path) no
  longer strips MEDIA tags out of fenced code blocks / inline-code examples;
  protected spans are a mask-locator, matching ``extract_media``.
* #53586 — ``_collect_history_media_paths`` also collects tags from
  assistant messages, and the post-stream delivery path filters against it.
"""

import os
from types import SimpleNamespace

import pytest

from gateway.platforms.base import (
    BasePlatformAdapter,
    MEDIA_DELIVERY_EXTS,
)
from gateway.run import _collect_history_media_paths


class TestGisExtensions:
    def test_gis_extensions_in_delivery_set(self):
        for ext in (".kmz", ".kml", ".geojson", ".gpx"):
            assert ext in MEDIA_DELIVERY_EXTS


class TestSpacedPaths:


    def test_spaced_path_followed_by_prose_keeps_prose(self, tmp_path):
        p = tmp_path / "my server.log"
        p.write_text("log line\n")
        media, cleaned = BasePlatformAdapter.extract_media(
            f"MEDIA:{p} is the log you asked for"
        )
        assert [os.path.realpath(x) for x, _ in media] == [os.path.realpath(str(p))]
        assert "is the log you asked for" in cleaned


    def test_forward_extension_stops_at_next_media_tag(self, tmp_path):
        a = tmp_path / "Caddyfile"
        b = tmp_path / "Dockerfile"
        a.write_text("localhost\n")
        b.write_text("FROM alpine\n")
        media, cleaned = BasePlatformAdapter.extract_media(
            f"MEDIA:{a} MEDIA:{b}"
        )
        got = sorted(os.path.realpath(x) for x, _ in media)
        assert got == sorted(
            [os.path.realpath(str(a)), os.path.realpath(str(b))]
        )
        assert "MEDIA:" not in cleaned


class TestStreamingDisplayStripCodeBlocks:
    def test_fenced_code_example_preserved(self, tmp_path):
        p = tmp_path / "real.pdf"
        p.write_text("x")
        text = f"Example:\n```\nMEDIA:{p}\n```\ndone MEDIA:{p}"
        out = BasePlatformAdapter.strip_media_directives_for_display(text)
        # The example inside the fence survives verbatim; the real tag outside
        # is stripped.
        assert f"MEDIA:{p}" in out
        assert out.count(f"MEDIA:{p}") == 1
        assert "```" in out


class TestHistoryMediaDedupe:
    def test_assistant_message_tags_collected(self):
        history = [
            {"role": "user", "content": "make a chart"},
            {"role": "assistant", "content": "Done! MEDIA:/tmp/chart.png"},
            {"role": "user", "content": "thanks"},
        ]
        paths = _collect_history_media_paths(history)
        assert "/tmp/chart.png" in paths

    def test_quoted_spaced_home_path_is_collected_in_delivery_form(
        self,
        tmp_path,
        monkeypatch,
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        history = [
            {
                "role": "assistant",
                "content": 'MEDIA:"~/audio cache/old.ogg"',
            },
        ]

        paths = _collect_history_media_paths(history)

        assert str(tmp_path / "audio cache" / "old.ogg") in paths

    def test_empty_history_empty_set(self):
        assert _collect_history_media_paths([]) == set()

    def test_current_turn_tts_tool_result_is_not_treated_as_delivered(self):
        class Store:
            def peek_session_id(self, _key):
                return "session-id"

            def load_transcript(self, _session_id):
                return [
                    {"role": "user", "content": "reply with TTS"},
                    {
                        "role": "assistant",
                        "tool_calls": [{
                            "id": "tts-call",
                            "function": {"name": "text_to_speech"},
                        }],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "tts-call",
                        "content": "[[audio_as_voice]]\nMEDIA:/tmp/new-voice.ogg",
                    },
                    {
                        "role": "assistant",
                        "content": "[[audio_as_voice]]\nMEDIA:/tmp/new-voice.ogg",
                    },
                ]

        adapter = SimpleNamespace(_session_store=Store())
        paths = BasePlatformAdapter._history_media_paths_for_session(adapter, "key")
        assert "/tmp/new-voice.ogg" not in (paths or set())

    def test_prior_turn_media_is_still_deduplicated(self):
        class Store:
            def peek_session_id(self, _key):
                return "session-id"

            def load_transcript(self, _session_id):
                return [
                    {"role": "user", "content": "make the first file"},
                    {"role": "assistant", "content": "MEDIA:/tmp/old.pdf"},
                    {"role": "user", "content": "make another file"},
                    {"role": "assistant", "content": "MEDIA:/tmp/new.pdf"},
                ]

        adapter = SimpleNamespace(_session_store=Store())
        paths = BasePlatformAdapter._history_media_paths_for_session(adapter, "key")
        assert paths == {"/tmp/old.pdf"}
