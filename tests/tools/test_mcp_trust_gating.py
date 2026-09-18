"""Tests for MCP tool trust-tier gating via readOnlyHint annotations.

Security boundary under test: write-capable MCP tools (anything whose
``readOnlyHint`` annotation is not exactly ``True``) on servers configured
``trust: untrusted`` must route through the existing dangerous-approval
path before the RPC fires. Read-only tools and tools on trusted servers
pass straight through.

Adversarial notes encoded in these tests:
- ``readOnlyHint`` is a HINT supplied by the (potentially hostile) server.
  It can only ever RELAX gating on a server the operator already marked
  untrusted; the trust tier itself is operator-side config, so a lying
  server can at worst skip approval for a tool it claims is read-only —
  which is why the trust key is per-server and gating is fail-closed for
  missing/unknown metadata.
- Missing annotations ⇒ write-capable (fail closed).
- Unknown/garbage ``trust`` values ⇒ treated as untrusted (fail closed).
"""

import asyncio
import hashlib
import hmac
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools import mcp_tool


class _FakeContentBlock:
    def __init__(self, text: str, block_type: str = "text"):
        self.text = text
        self.type = block_type


class _FakeCallToolResult:
    def __init__(self, content, is_error=False, structuredContent=None):
        self.content = content
        self.isError = is_error
        self.structuredContent = structuredContent


def _fake_run_on_mcp_loop(coro_or_factory, timeout=30):
    coro = coro_or_factory() if callable(coro_or_factory) else coro_or_factory
    loop = asyncio.new_event_loop()
    try:
        async def _install_lock_and_run():
            for srv in list(mcp_tool._servers.values()):
                if getattr(srv, "_rpc_lock", None) is None:
                    srv._rpc_lock = asyncio.Lock()
            return await coro
        return loop.run_until_complete(_install_lock_and_run())
    finally:
        loop.close()


@pytest.fixture
def fake_session():
    """Patch a fake connected server + MCP loop; yield its session mock."""
    session = MagicMock()
    session.call_tool = AsyncMock(
        return_value=_FakeCallToolResult(content=[_FakeContentBlock("ok")])
    )
    server = SimpleNamespace(session=session, _rpc_lock=None)
    with patch.dict(mcp_tool._servers, {"srv": server}), \
         patch("tools.mcp_tool._run_on_mcp_loop",
               side_effect=_fake_run_on_mcp_loop), \
         patch.dict(mcp_tool._server_error_counts, {}, clear=True):
        yield session


@pytest.fixture(autouse=True)
def _clean_trust_state():
    """Isolate the module-level trust metadata between tests."""
    with patch.dict(mcp_tool._server_trust_levels, {}, clear=True), \
         patch.dict(mcp_tool._tool_read_only_hints, {}, clear=True):
        yield


def _set_trust(server: str, trust: str):
    mcp_tool._server_trust_levels[server] = trust


def _set_read_only(server: str, tool: str, value: bool):
    mcp_tool._tool_read_only_hints.setdefault(server, {})[tool] = value


class TestTrustGateAtCallTime:
    """The handler preamble consults the approval path when required."""

    def test_write_capable_on_untrusted_server_requires_approval(
        self, fake_session
    ):
        """Approval shows exact canonical args; 'accept' dispatches them."""
        _set_trust("srv", "untrusted")
        # No readOnlyHint recorded for delete_repo → write-capable.
        handler = mcp_tool._make_tool_handler("srv", "delete_repo", 30.0)
        with patch(
            "tools.approval.request_elicitation_consent",
            return_value="accept",
        ) as consent:
            raw = handler({"z": [2, 1], "repo": "x"})
        consent.assert_called_once()
        message, description = consent.call_args.args
        assert '{"repo":"x","z":[2,1]}' in message
        assert "MCP argument binding HMAC-SHA-256:" in message
        assert "same frozen argument snapshot" in description
        assert json.loads(raw) == {"result": "ok"}
        fake_session.call_tool.assert_awaited_once_with(
            "delete_repo", arguments={"repo": "x", "z": [2, 1]}
        )

    def test_approval_and_rpc_share_snapshot_when_original_args_mutate(
        self, fake_session
    ):
        """Approval callback cannot swap values in the later RPC payload."""
        _set_trust("srv", "untrusted")
        handler = mcp_tool._make_tool_handler("srv", "send_message", 30.0)
        args = {
            "to": ["approved@example.com"],
            "subject": "Approved subject",
            "body": "Approved body",
        }

        def approve_then_mutate(message, description, **kwargs):
            assert (
                '{"body":"Approved body","subject":"Approved subject",'
                '"to":["approved@example.com"]}'
            ) in message
            args["to"] = ["substituted@example.com"]
            args["subject"] = "Substituted subject"
            args["body"] = "Substituted body"
            return "accept"

        with patch(
            "tools.approval.request_elicitation_consent",
            side_effect=approve_then_mutate,
        ):
            raw = handler(args)

        assert json.loads(raw) == {"result": "ok"}
        fake_session.call_tool.assert_awaited_once_with(
            "send_message",
            arguments={
                "to": ["approved@example.com"],
                "subject": "Approved subject",
                "body": "Approved body",
            },
        )

    def test_non_json_arguments_fail_before_approval_or_rpc(self, fake_session):
        """No reviewer or transport sees an argument set Hermes cannot bind."""
        _set_trust("srv", "untrusted")
        handler = mcp_tool._make_tool_handler("srv", "send_message", 30.0)
        with patch(
            "tools.approval.request_elicitation_consent"
        ) as consent:
            raw = handler({"body": float("nan")})

        consent.assert_not_called()
        fake_session.call_tool.assert_not_awaited()
        assert "valid JSON object" in json.loads(raw)["error"]

    def test_secret_redaction_preserves_raw_snapshot_binding(self, fake_session):
        """Reviewer sees a mask plus the digest of the exact dispatched JSON."""
        _set_trust("srv", "untrusted")
        handler = mcp_tool._make_tool_handler("srv", "store_token", 30.0)
        token = "ghp_abcdefghijklmnopqrstuvwxyz1234567890"
        canonical = json.dumps(
            {"token": token},
            sort_keys=True,
            separators=(",", ":"),
        )

        with patch(
            "tools.approval.request_elicitation_consent",
            return_value="decline",
        ) as consent:
            raw = handler({"token": token})

        message = consent.call_args.args[0]
        assert token not in message
        assert "***" in message
        public_digest = hashlib.sha256(canonical.encode()).hexdigest()
        keyed_binding = hmac.new(
            mcp_tool._MCP_APPROVAL_BINDING_KEY,
            canonical.encode(),
            hashlib.sha256,
        ).hexdigest()
        assert public_digest not in message
        assert keyed_binding in message
        fake_session.call_tool.assert_not_awaited()
        assert "did not approve" in json.loads(raw)["error"]

    def test_real_gateway_approval_payload_matches_dispatched_snapshot(
        self, fake_session
    ):
        """Exercise handler -> approval queue -> decision -> native RPC."""
        from tools import approval

        _set_trust("srv", "untrusted")
        handler = mcp_tool._make_tool_handler("srv", "send_message", 30.0)
        notices = []
        session_key = "mcp-binding-gateway"

        def notify(data):
            notices.append(data)
            assert approval.resolve_gateway_approval(
                session_key,
                "once",
                request_id="not-this-request",
            ) == 0
            assert approval.resolve_gateway_approval(
                session_key,
                "once",
                request_id=data["request_id"],
            ) == 1

        with (
            patch(
                "tools.approval.get_current_session_key",
                return_value=session_key,
            ),
            patch(
                "tools.approval._is_gateway_approval_context",
                return_value=True,
            ),
            patch.dict(
                approval._gateway_notify_cbs,
                {session_key: notify},
                clear=True,
            ),
            patch.dict(approval._gateway_queues, {}, clear=True),
        ):
            raw = handler(
                {
                    "to": ["owner@example.com"],
                    "subject": "Review me",
                    "body": "Exact body",
                }
            )

        expected = (
            '{"body":"Exact body","subject":"Review me",'
            '"to":["owner@example.com"]}'
        )
        assert len(notices) == 1
        assert expected in notices[0]["command"]
        assert notices[0]["pattern_key"] == "mcp_elicitation"
        assert notices[0]["allow_session"] is False
        assert notices[0]["allow_permanent"] is False
        assert notices[0]["coalesce"] is False
        assert notices[0]["requires_full_review"] is True
        assert notices[0]["binding_summary"].startswith(
            "MCP argument binding HMAC-SHA-256:"
        )
        fake_session.call_tool.assert_awaited_once_with(
            "send_message",
            arguments={
                "to": ["owner@example.com"],
                "subject": "Review me",
                "body": "Exact body",
            },
        )
        assert json.loads(raw) == {"result": "ok"}

    @pytest.mark.parametrize("decision", ["deny", "expire"])
    def test_real_gateway_denial_and_expiry_never_dispatch(
        self, fake_session, decision
    ):
        """A refusal or silence closes the exact request before transport."""
        from tools import approval

        _set_trust("srv", "untrusted")
        handler = mcp_tool._make_tool_handler("srv", "send_message", 30.0)
        notices = []
        session_key = f"mcp-binding-{decision}"

        def notify(data):
            notices.append(data)
            if decision == "deny":
                assert approval.resolve_gateway_approval(
                    session_key,
                    "deny",
                    request_id=data["request_id"],
                ) == 1

        with (
            patch(
                "tools.approval.get_current_session_key",
                return_value=session_key,
            ),
            patch(
                "tools.approval._is_gateway_approval_context",
                return_value=True,
            ),
            patch(
                "tools.approval._get_approval_timeout",
                return_value=0 if decision == "expire" else 60,
            ),
            patch.dict(
                approval._gateway_notify_cbs,
                {session_key: notify},
                clear=True,
            ),
            patch.dict(approval._gateway_queues, {}, clear=True),
            patch.dict(approval._gateway_expired, {}, clear=True),
        ):
            raw = handler(
                {
                    "to": ["owner@example.com"],
                    "subject": "Must not send",
                    "body": "Must not send",
                }
            )

        assert len(notices) == 1
        assert notices[0]["request_id"]
        assert '"subject":"Must not send"' in notices[0]["command"]
        fake_session.call_tool.assert_not_awaited()
        assert "did not approve" in json.loads(raw)["error"]

    def test_identical_concurrent_consents_keep_distinct_request_ids(self):
        """One request's answer cannot authorize a concurrent twin."""
        from tools import approval

        session_key = "mcp-binding-concurrent"
        notices = []
        two_notices = threading.Event()
        results = []

        def notify(data):
            notices.append(data)
            if len(notices) == 2:
                two_notices.set()

        def request():
            results.append(
                approval.request_elicitation_consent(
                    "same exact operation",
                    "one-request consent",
                )
            )

        with (
            patch(
                "tools.approval.get_current_session_key",
                return_value=session_key,
            ),
            patch(
                "tools.approval._is_gateway_approval_context",
                return_value=True,
            ),
            patch("tools.approval._get_approval_timeout", return_value=2),
            patch.dict(
                approval._gateway_notify_cbs,
                {session_key: notify},
                clear=True,
            ),
            patch.dict(approval._gateway_queues, {}, clear=True),
            patch.dict(approval._gateway_expired, {}, clear=True),
        ):
            threads = [threading.Thread(target=request) for _ in range(2)]
            for thread in threads:
                thread.start()
            assert two_notices.wait(timeout=1)
            request_ids = [notice["request_id"] for notice in notices]
            assert len(set(request_ids)) == 2
            assert approval.resolve_gateway_approval(
                session_key, "once", request_id=request_ids[0]
            ) == 1
            assert approval.resolve_gateway_approval(
                session_key, "deny", request_id=request_ids[1]
            ) == 1
            for thread in threads:
                thread.join(timeout=1)
                assert not thread.is_alive()

        assert sorted(results) == ["accept", "decline"]

    def test_denied_approval_blocks_rpc(self, fake_session):
        """'decline' blocks the call — the RPC must never fire."""
        _set_trust("srv", "untrusted")
        handler = mcp_tool._make_tool_handler("srv", "delete_repo", 30.0)
        with patch(
            "tools.approval.request_elicitation_consent",
            return_value="decline",
        ):
            raw = handler({"repo": "x"})
        fake_session.call_tool.assert_not_awaited()
        assert "error" in json.loads(raw)
        assert "did not approve" in json.loads(raw)["error"]

    def test_read_only_tool_on_untrusted_server_skips_approval(
        self, fake_session
    ):
        """readOnlyHint=True tools pass without consulting approval."""
        _set_trust("srv", "untrusted")
        _set_read_only("srv", "list_repos", True)
        handler = mcp_tool._make_tool_handler("srv", "list_repos", 30.0)
        with patch(
            "tools.approval.request_elicitation_consent"
        ) as consent:
            raw = handler({})
        consent.assert_not_called()
        assert json.loads(raw) == {"result": "ok"}

    def test_trusted_server_skips_approval_for_write_tools(
        self, fake_session
    ):
        """trust: full (and the default) never consults approval."""
        _set_trust("srv", "full")
        handler = mcp_tool._make_tool_handler("srv", "delete_repo", 30.0)
        with patch(
            "tools.approval.request_elicitation_consent"
        ) as consent:
            raw = handler({"repo": "x"})
        consent.assert_not_called()
        assert json.loads(raw) == {"result": "ok"}

    def test_unconfigured_server_defaults_to_full_trust(self, fake_session):
        """Backward compat: servers with no trust key behave as before."""
        handler = mcp_tool._make_tool_handler("srv", "delete_repo", 30.0)
        with patch(
            "tools.approval.request_elicitation_consent"
        ) as consent:
            raw = handler({"repo": "x"})
        consent.assert_not_called()
        assert json.loads(raw) == {"result": "ok"}

    def test_read_only_false_hint_is_gated(self, fake_session):
        """An explicit readOnlyHint=False is write-capable."""
        _set_trust("srv", "untrusted")
        _set_read_only("srv", "write_file", False)
        handler = mcp_tool._make_tool_handler("srv", "write_file", 30.0)
        with patch(
            "tools.approval.request_elicitation_consent",
            return_value="decline",
        ) as consent:
            handler({"path": "/etc/passwd"})
        consent.assert_called_once()
        fake_session.call_tool.assert_not_awaited()

    def test_approval_exception_fails_closed(self, fake_session):
        """Any exception in the consent path blocks the call."""
        _set_trust("srv", "untrusted")
        handler = mcp_tool._make_tool_handler("srv", "delete_repo", 30.0)
        with patch(
            "tools.approval.request_elicitation_consent",
            side_effect=RuntimeError("approval backend down"),
        ):
            raw = handler({"repo": "x"})
        fake_session.call_tool.assert_not_awaited()
        assert "error" in json.loads(raw)


class TestTrustNormalization:
    def test_unknown_trust_value_treated_as_untrusted(self):
        """Garbage trust strings fail closed to untrusted."""
        assert mcp_tool._normalize_server_trust("banana") == "untrusted"

    def test_known_values(self):
        assert mcp_tool._normalize_server_trust("full") == "full"
        assert mcp_tool._normalize_server_trust("UNTRUSTED") == "untrusted"
        assert mcp_tool._normalize_server_trust("  Full ") == "full"
        # Missing key → default full (backward compatible; documented).
        assert mcp_tool._normalize_server_trust(None) == "full"


class TestAnnotationCaptureAtDiscovery:
    """_register_server_tools records trust + readOnlyHint metadata."""

    def _make_tool(self, name, annotations=None):
        return SimpleNamespace(
            name=name, description="", inputSchema=None,
            annotations=annotations,
        )

    def test_registration_records_hints_and_trust(self):
        from tools.registry import ToolRegistry

        server = mcp_tool.MCPServerTask("srv")
        server.session = MagicMock()
        server._tools = [
            self._make_tool(
                "list_repos", SimpleNamespace(readOnlyHint=True)
            ),
            self._make_tool(
                "delete_repo", SimpleNamespace(readOnlyHint=False)
            ),
            self._make_tool("no_annotations", None),
        ]
        config = {
            "trust": "untrusted",
            "tools": {"resources": False, "prompts": False},
        }
        with patch("tools.registry.registry", ToolRegistry()), \
             patch("tools.mcp_tool._track_mcp_tool_server"):
            mcp_tool._register_server_tools("srv", server, config)

        assert mcp_tool._server_trust_levels["srv"] == "untrusted"
        hints = mcp_tool._tool_read_only_hints["srv"]
        assert hints.get("list_repos") is True
        # Anything not exactly True is write-capable.
        assert not hints.get("delete_repo")
        assert not hints.get("no_annotations")

    def test_dict_annotations_supported(self):
        """Cached/JSON annotations arrive as plain dicts."""
        assert mcp_tool._annotation_read_only_hint(
            SimpleNamespace(annotations={"readOnlyHint": True})
        ) is True
        assert mcp_tool._annotation_read_only_hint(
            SimpleNamespace(annotations={"readOnlyHint": "yes"})
        ) is False  # non-bool truthy → NOT read-only (hint must be True)
        assert mcp_tool._annotation_read_only_hint(
            SimpleNamespace(annotations=None)
        ) is False
        assert mcp_tool._annotation_read_only_hint(
            SimpleNamespace()
        ) is False
