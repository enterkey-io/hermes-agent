"""Malformed model tool arguments are rejected at the dispatch boundary."""

import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent


def _make_agent() -> AIAgent:
    tool_defs = [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "search",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    with (
        patch("run_agent.get_tool_definitions", return_value=tool_defs),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("hermes_cli.config.load_config", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent._flush_messages_to_session_db = MagicMock()
    return agent


def _tool_call(call_id: str, arguments, *, name: str = "web_search"):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


@pytest.mark.parametrize("dispatch_mode", ["sequential", "concurrent"])
@pytest.mark.parametrize(
    "bad_arguments",
    [
        pytest.param("not-json", id="malformed-json"),
        pytest.param('"scalar"', id="scalar"),
        pytest.param("[]", id="list"),
        pytest.param("", id="empty"),
        pytest.param('{"query": "cut off', id="truncated"),
    ],
)
def test_malformed_arguments_are_rejected_without_blocking_valid_sibling(
    dispatch_mode: str,
    bad_arguments: str,
):
    agent = _make_agent()
    assistant_message = SimpleNamespace(
        content="",
        tool_calls=[
            _tool_call("call-bad", bad_arguments),
            _tool_call("call-good", '{"query": "valid"}'),
        ],
    )
    messages = []
    executed = []

    def fake_dispatch(name, args, task_id, *positional, **kwargs):
        call_id = kwargs.get("tool_call_id") or (positional[0] if positional else None)
        executed.append((name, args, call_id))
        return json.dumps({"ok": args["query"]})

    with (
        patch("run_agent.handle_function_call", side_effect=fake_dispatch),
        patch.object(agent, "_invoke_tool", side_effect=fake_dispatch),
        patch(
            "agent.tool_executor.maybe_persist_tool_result",
            side_effect=lambda **kwargs: kwargs["content"],
        ),
    ):
        execute = getattr(agent, f"_execute_tool_calls_{dispatch_mode}")
        execute(assistant_message, messages, "task-1")

    assert executed == [("web_search", {"query": "valid"}, "call-good")]
    assert [message["tool_call_id"] for message in messages] == ["call-bad", "call-good"]
    assert len([message for message in messages if message["tool_call_id"] == "call-bad"]) == 1

    assert '"error": "Invalid tool arguments"' in messages[0]["content"]
    assert "JSON object" in messages[0]["content"]
    assert json.loads(messages[1]["content"]) == {"ok": "valid"}


@pytest.mark.parametrize("dispatch_mode", ["sequential", "concurrent"])
@pytest.mark.parametrize("bad_arguments", ["not-json", '"scalar"', "[]", None])
def test_malformed_required_call_records_failure_in_both_executor_paths(
    dispatch_mode: str,
    bad_arguments,
):
    from tools.required_dependency_runtime import activate, reset

    agent = _make_agent()
    assistant_message = SimpleNamespace(
        content="",
        tool_calls=[_tool_call("call-bad", bad_arguments, name="terminal")],
    )
    messages = []
    token, state = activate(["terminal"])
    try:
        execute = getattr(agent, f"_execute_tool_calls_{dispatch_mode}")
        execute(assistant_message, messages, "task-1")
        summary = state.finalize()
    finally:
        reset(token)

    assert len(messages) == 1
    assert "tool was not executed" in messages[0]["content"].lower()
    assert summary["successful"] == []
    assert summary["missing"] == []
    assert summary["failed"] == [
        {"tool": "terminal", "reasons": ["invalid_arguments"]}
    ]


@pytest.mark.parametrize("dispatch_mode", ["sequential", "concurrent"])
@pytest.mark.parametrize("nested_case", ["malformed", "missing_required"])
def test_rejected_bridge_call_records_underlying_required_dependency(
    dispatch_mode: str,
    nested_case: str,
):
    from tools.registry import registry
    from tools.required_dependency_runtime import activate, reset

    name = f"mcp__required__{dispatch_mode}_{nested_case}"
    registry.register(
        name=name,
        toolset="mcp-required-test",
        schema={
            "name": name,
            "description": "required bridge test",
            "parameters": {
                "type": "object",
                "properties": {"document_id": {"type": "string"}},
                "required": ["document_id"],
            },
        },
        handler=lambda _args, **_kwargs: pytest.fail("rejected bridge dispatched"),
    )
    nested_arguments = "not-json" if nested_case == "malformed" else {}
    agent = _make_agent()
    assistant_message = SimpleNamespace(
        content="",
        tool_calls=[
            _tool_call(
                "call-bridge",
                json.dumps({"name": name, "arguments": nested_arguments}),
                name="tool_call",
            )
        ],
    )
    messages = []
    token, state = activate([name])
    try:
        with patch(
            "agent.tool_executor._tool_search_scoped_names",
            return_value=frozenset({name}),
        ):
            execute = getattr(agent, f"_execute_tool_calls_{dispatch_mode}")
            execute(assistant_message, messages, "task-1")
        summary = state.finalize()
    finally:
        reset(token)

    assert len(messages) == 1
    assert summary["missing"] == []
    assert summary["failed"] == [
        {"tool": name, "reasons": ["executor_blocked"]}
    ]


def test_queued_required_call_timeout_records_failure():
    from tools.required_dependency_runtime import activate, reset

    agent = _make_agent()
    assistant_message = SimpleNamespace(
        content="",
        tool_calls=[
            _tool_call("call-blocker", '{"query":"wait"}'),
            _tool_call("call-required", '{"command":"/usr/bin/true"}', name="terminal"),
        ],
    )
    messages = []
    release = threading.Event()
    finished = threading.Event()

    def invoke(name, _args, _task_id, _call_id, **_kwargs):
        if name == "terminal":
            pytest.fail("queued required call dispatched after timeout")
        try:
            release.wait(timeout=2)
            return json.dumps({"ok": True})
        finally:
            finished.set()

    token, state = activate(["terminal"])
    try:
        with (
            patch.object(agent, "_invoke_tool", side_effect=invoke),
            patch("agent.tool_executor._max_workers_for_tool_batch", return_value=1),
            patch("agent.tool_executor._resolve_concurrent_tool_timeout", return_value=0.02),
        ):
            agent._execute_tool_calls_concurrent(
                assistant_message,
                messages,
                "task-1",
            )
        summary = state.finalize()
    finally:
        release.set()
        finished.wait(timeout=2)
        reset(token)

    assert len(messages) == 2
    assert "timed out" in messages[1]["content"].lower()
    assert summary["missing"] == []
    assert summary["failed"] == [
        {"tool": "terminal", "reasons": ["executor_timeout"]}
    ]


def test_review_handoff_skipped_bridge_records_underlying_dependency():
    import agent.tool_executor as tool_executor
    from tools.registry import registry
    from tools.required_dependency_runtime import activate, reset

    name = "mcp__required__review_handoff"
    dispatched = []
    registry.register(
        name=name,
        toolset="mcp-required-test",
        schema={
            "name": name,
            "description": "required bridge handoff test",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=lambda _args, **_kwargs: dispatched.append(True),
    )
    agent = _make_agent()
    skipped = _tool_call(
        "call-skipped-bridge",
        json.dumps({"name": name, "arguments": {}}),
        name="tool_call",
    )
    messages = []
    token, state = activate([name])
    try:
        with patch(
            "agent.tool_executor._tool_search_scoped_names",
            return_value=frozenset({name}),
        ):
            assert tool_executor._append_work_review_skipped_tool_results(
                agent,
                messages,
                [skipped],
            )
        summary = state.finalize()
    finally:
        reset(token)

    assert dispatched == []
    assert len(messages) == 1
    assert summary["missing"] == []
    assert summary["failed"] == [
        {"tool": name, "reasons": ["review_handoff_skipped"]}
    ]
