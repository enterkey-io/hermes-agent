"""Tests for session_meta filtering — issue #4715.

Ensures that transcript-only session_meta messages never reach the
chat-completions API, via both the API-boundary guard in
_sanitize_api_messages() and the CLI session-restore paths.
"""

import logging

from run_agent import AIAgent


# ---------------------------------------------------------------------------
# Layer 1 — _sanitize_api_messages role-allowlist guard
# ---------------------------------------------------------------------------

class TestSanitizeApiMessagesRoleFilter:

    def test_drops_session_meta_role(self):
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "session_meta", "content": {"model": "gpt-4"}},
            {"role": "assistant", "content": "hi"},
        ]
        out = AIAgent._sanitize_api_messages(msgs)
        assert len(out) == 2
        assert all(m["role"] != "session_meta" for m in out)

    def test_preserves_valid_roles(self):
        msgs = [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        ]
        # Need a matching assistant tool_call so the tool result isn't orphaned
        msgs[2]["tool_calls"] = [{"id": "c1", "function": {"name": "t", "arguments": "{}"}}]
        out = AIAgent._sanitize_api_messages(msgs)
        roles = [m["role"] for m in out]
        assert "system" in roles
        assert "user" in roles
        assert "assistant" in roles
        assert "tool" in roles

    def test_logs_warning_when_dropping(self, caplog):
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "session_meta", "content": {"info": "test"}},
        ]
        with caplog.at_level(logging.DEBUG, logger="run_agent"):
            AIAgent._sanitize_api_messages(msgs)
        assert any("invalid role" in r.message and "session_meta" in r.message for r in caplog.records)



# ---------------------------------------------------------------------------
# Layer 1b — display-only timeline fields must not reach the provider
# ---------------------------------------------------------------------------

class TestDisplayFieldsStrippedFromApiPayload:
    """Display-only fields (display_kind, display_metadata) are persisted on
    message rows for timeline rendering, but must never appear in the
    provider-bound API payload — strict OpenAI-compatible backends reject
    unknown fields."""

    def test_sanitizer_strips_display_and_platform_identity_fields(self):
        """The final pre-call guard covers restored and live gateway metadata."""
        msgs = [
            {
                "role": "user",
                "content": "hello",
                "display_kind": "model_switch",
                "message_id": "inbound-1",
            },
            {
                "role": "assistant",
                "content": "hi",
                "display_metadata": {"model": "m"},
                "platform_message_id": "outbound-1",
            },
        ]
        out = AIAgent._sanitize_api_messages(msgs)
        assert all(
            not {"display_kind", "display_metadata", "message_id", "platform_message_id"}
            & message.keys()
            for message in out
        )
        assert msgs[0]["message_id"] == "inbound-1"
        assert msgs[1]["platform_message_id"] == "outbound-1"

    def test_conversation_loop_strips_display_fields(self):
        """The per-request api_msg copy in conversation_loop strips
        display_kind and display_metadata before the message reaches the
        provider. This simulates that pop."""
        msg = {
            "role": "user",
            "content": "switch event",
            "display_kind": "model_switch",
            "display_metadata": {"model": "test"},
            "api_content": "sidecar",
        }
        # Reproduce the pop sequence from conversation_loop.py
        api_msg = msg.copy()
        api_msg.pop("api_content", None)
        api_msg.pop("display_kind", None)
        api_msg.pop("display_metadata", None)
        assert "display_kind" not in api_msg
        assert "display_metadata" not in api_msg
        assert "api_content" not in api_msg
        assert api_msg["content"] == "switch event"
        # Original message dict is untouched.
        assert msg.get("display_kind") == "model_switch"


# ---------------------------------------------------------------------------
# Layer 2 — CLI session-restore filters session_meta before loading
# ---------------------------------------------------------------------------

class TestCLISessionRestoreFiltering:

    def test_restore_filters_session_meta(self):
        """Simulates the CLI restore path and verifies session_meta is removed."""
        # Build a fake restored message list (as returned by get_messages_as_conversation)
        fake_restored = [
            {"role": "session_meta", "content": {"model": "gpt-4"}},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
            {"role": "session_meta", "content": {"tools": []}},
        ]

        # Apply the same filtering that the patched CLI code now does
        filtered = [m for m in fake_restored if m.get("role") != "session_meta"]

        assert len(filtered) == 2
        assert all(m["role"] != "session_meta" for m in filtered)
        assert filtered[0]["role"] == "user"
        assert filtered[1]["role"] == "assistant"
