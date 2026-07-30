"""Agent-path tests for protected tool schema and invocation grants."""

from __future__ import annotations

import json
import types
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import model_tools
from agent.agent_runtime_helpers import invoke_tool
from agent.execution_capabilities import (
    _bind_execution_context,
    _issue_cron_job_execution_context,
    _issue_tool_invocation_grant,
    _issue_trusted_cron_dispatch,
    cron_job_capability,
)
from agent.tool_executor import _parse_tool_arguments
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from run_agent import AIAgent
from tools import mcp_tool
from tools.registry import registry


JOB_ID = "8ff7fa2ddb8b"
TOOL_NAME = "_test_agent_path_protected_tool"
POLICY_TOOL_NAME = "_test_agent_path_strict_json_tool"


class _Agent:
    def __init__(self, execution_context=None):
        self.execution_context = execution_context
        self.session_id = "test-session"
        self.valid_tool_names = {TOOL_NAME}
        self.enabled_toolsets = None
        self.disabled_toolsets = None
        self._current_turn_id = "turn-1"
        self._current_api_request_id = "request-1"
        self.clarify_callback = None
        self.read_terminal_callback = None
        self._memory_manager = None


def _requirement(home):
    return cron_job_capability(
        profile_name="emily",
        hermes_home=home,
        job_id=JOB_ID,
    )


def _context(home, *, owner=None):
    job = {"id": JOB_ID}
    permit = _issue_trusted_cron_dispatch(
        job=job,
        profile_name="emily",
        hermes_home=home,
        execution_id="execution-1",
        allowed_tools={TOOL_NAME},
        protected_handler_timeout_seconds=1.0,
    )
    context = _issue_cron_job_execution_context(
        permit=permit,
        job=job,
        execution_id="execution-1",
        profile_name="emily",
        hermes_home=home,
    )
    if owner is not None:
        _bind_execution_context(context, owner)
    return context


def _register(home, handler):
    registry.register(
        name=TOOL_NAME,
        toolset="test-capability",
        schema={
            "name": TOOL_NAME,
            "description": "Protected",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=handler,
        execution_capability=_requirement(home),
    )


def test_model_tool_definitions_require_exact_context_and_owner(tmp_path):
    home = tmp_path / "profiles" / "emily"
    home.mkdir(parents=True)
    _register(home, lambda args, **kwargs: '{"ok": true}')
    exact_owner = object()
    exact_context = _context(home, owner=exact_owner)
    try:
        absent = model_tools.get_tool_definitions(
            enabled_toolsets=["test-capability"],
            quiet_mode=True,
        )
        copied_without_owner = model_tools.get_tool_definitions(
            enabled_toolsets=["test-capability"],
            quiet_mode=True,
            execution_context=exact_context,
        )
        wrong_owner = model_tools.get_tool_definitions(
            enabled_toolsets=["test-capability"],
            quiet_mode=True,
            execution_context=exact_context,
            execution_owner=object(),
        )
        present = model_tools.get_tool_definitions(
            enabled_toolsets=["test-capability"],
            quiet_mode=True,
            execution_context=exact_context,
            execution_owner=exact_owner,
        )
    finally:
        registry.deregister(TOOL_NAME)
        model_tools._clear_tool_defs_cache()

    names = lambda defs: {item["function"]["name"] for item in defs}
    assert TOOL_NAME not in names(absent)
    assert TOOL_NAME not in names(copied_without_owner)
    assert TOOL_NAME not in names(wrong_owner)
    assert TOOL_NAME in names(present)


def test_real_agent_helper_mints_grant_but_direct_and_nested_dispatch_fail(tmp_path):
    home = tmp_path / "profiles" / "emily"
    home.mkdir(parents=True)
    calls = []
    _register(
        home,
        lambda args, **kwargs: (
            calls.append(kwargs["execution_runtime"])
            or json.dumps({"ok": True})
        ),
    )
    agent = _Agent()
    context = _context(home, owner=agent)
    agent.execution_context = context
    try:
        direct = json.loads(
            model_tools.handle_function_call(
                TOOL_NAME,
                {},
                task_id="direct",
                skip_pre_tool_call_hook=True,
                skip_tool_request_middleware=True,
            )
        )
        nested = json.loads(
            invoke_tool(
                _Agent(context),
                TOOL_NAME,
                {},
                effective_task_id="nested-agent",
                tool_call_id="call-nested",
                skip_tool_request_middleware=True,
            )
        )
        result = json.loads(
            invoke_tool(
                agent,
                TOOL_NAME,
                {},
                effective_task_id="agent-path",
                tool_call_id="call-1",
                skip_tool_request_middleware=True,
            )
        )
    finally:
        registry.deregister(TOOL_NAME)

    assert direct["error_type"] == "execution_capability_unavailable"
    assert nested["error_type"] == "execution_capability_unavailable"
    assert result == {"ok": True}
    assert len(calls) == 1


def test_agent_without_execution_context_cannot_mint_protected_grant(tmp_path):
    home = tmp_path / "profiles" / "emily"
    home.mkdir(parents=True)
    _register(home, lambda args, **kwargs: '{"ok": true}')
    try:
        result = json.loads(
            invoke_tool(
                _Agent(),
                TOOL_NAME,
                {},
                effective_task_id="general-chat",
                tool_call_id="call-1",
                skip_tool_request_middleware=True,
            )
        )
    finally:
        registry.deregister(TOOL_NAME)

    assert result["error_type"] == "execution_capability_unavailable"


def test_model_raw_json_policy_rejects_nested_duplicates_before_dispatch(
    tmp_path,
):
    home = tmp_path / "profiles" / "emily"
    home.mkdir(parents=True)
    calls = []
    plugin_context = PluginContext(
        PluginManifest(
            name="Emily Paperclip Job",
            key="emily-paperclip-job",
            source="user",
        ),
        PluginManager(),
    )
    plugin_context.require_inbound_json_policy(
        reject_duplicate_object_keys=True,
    )
    requirement = plugin_context.cron_job_capability(
        profile_name="emily",
        hermes_home=home,
        job_id=JOB_ID,
    )
    plugin_context._register_tool_in(
        registry,
        name=POLICY_TOOL_NAME,
        toolset="test-capability",
        schema={
            "name": POLICY_TOOL_NAME,
            "description": "Strict protected input",
            "parameters": {
                "type": "object",
                "properties": {
                    "payload": {
                        "type": "object",
                        "properties": {
                            "title": {
                                "type": "string",
                                "pattern": (
                                    "^[^\\u0000-\\u001f"
                                    "\\u007f-\\u009f]*$"
                                ),
                            }
                        },
                    }
                },
            },
        },
        handler=lambda args, **kwargs: calls.append(args) or '{"ok":true}',
        execution_capability=requirement,
    )
    job = {"id": JOB_ID}
    permit = _issue_trusted_cron_dispatch(
        job=job,
        profile_name="emily",
        hermes_home=home,
        execution_id="execution-strict-json",
        allowed_tools={POLICY_TOOL_NAME},
        protected_handler_timeout_seconds=1.0,
        requirement=requirement,
    )
    execution_context = _issue_cron_job_execution_context(
        permit=permit,
        job=job,
        execution_id="execution-strict-json",
        profile_name="emily",
        hermes_home=home,
        requirement=requirement,
    )
    agent = _Agent(execution_context)
    agent.valid_tool_names.add(POLICY_TOOL_NAME)
    _bind_execution_context(execution_context, agent)

    try:
        parsed, duplicate_error, duplicate_admission = _parse_tool_arguments(
            '{"payload":{"title":"first","title":"second"}}',
            function_name=POLICY_TOOL_NAME,
            agent=agent,
        )
        grant = _issue_tool_invocation_grant(
            execution_context,
            owner=agent,
            tool_name=POLICY_TOOL_NAME,
        )
        direct_without_raw_proof = json.loads(
            model_tools.handle_function_call(
                POLICY_TOOL_NAME,
                {"payload": {"title": "direct"}},
                task_id="direct-model-dispatch",
                skip_pre_tool_call_hook=True,
                skip_tool_request_middleware=True,
                _execution_capability_grant=grant,
                _execution_capability_owner=agent,
                _execution_context=execution_context,
            )
        )
    finally:
        registry.deregister(POLICY_TOOL_NAME)
        model_tools._clear_tool_defs_cache()

    assert parsed == {}
    assert json.loads(duplicate_error)["error_type"] == "inbound_json_policy"
    assert duplicate_admission is None
    assert direct_without_raw_proof["error_type"] == (
        "inbound_json_policy_unavailable"
    )
    assert calls == []


def test_mcp_refresh_preserves_exact_execution_context_and_owner(monkeypatch):
    agent = _Agent(execution_context=object())
    agent.tools = []
    agent.valid_tool_names = set()
    agent._tool_snapshot_generation = 0
    seen = {}

    def capture(**kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(model_tools, "get_tool_definitions", capture)
    monkeypatch.setattr(
        mcp_tool,
        "_reinject_post_build_tools",
        lambda agent, defs, names: set(),
    )

    mcp_tool.refresh_agent_mcp_tools(agent)

    assert seen["execution_context"] is agent.execution_context
    assert seen["execution_owner"] is agent


def test_uncertain_invocation_closes_context_before_second_protected_call(tmp_path):
    home = tmp_path / "profiles" / "emily"
    home.mkdir(parents=True)
    calls = []

    def handler(args, *, execution_runtime, **kwargs):
        calls.append(args)
        execution_runtime.mark_external_mutation_started("issue-create-uncertain")
        raise TimeoutError("response not received")

    _register(home, handler)
    agent = _Agent()
    context = _context(home, owner=agent)
    agent.execution_context = context
    try:
        first = json.loads(
            invoke_tool(
                agent,
                TOOL_NAME,
                {"attempt": 1},
                effective_task_id="agent-path",
                tool_call_id="call-1",
                skip_tool_request_middleware=True,
            )
        )
        refreshed = model_tools.get_tool_definitions(
            enabled_toolsets=["test-capability"],
            quiet_mode=True,
            execution_context=context,
            execution_owner=agent,
        )
        second = json.loads(
            invoke_tool(
                agent,
                TOOL_NAME,
                {"attempt": 2},
                effective_task_id="agent-path",
                tool_call_id="call-2",
                skip_tool_request_middleware=True,
            )
        )
    finally:
        registry.deregister(TOOL_NAME)
        model_tools._clear_tool_defs_cache()

    names = {item["function"]["name"] for item in refreshed}
    assert first["error_type"] == "protected_mutation_uncertain"
    assert first["reconciliation_required"] is True
    assert TOOL_NAME not in names
    assert second["error_type"] == "execution_capability_unavailable"
    assert calls == [{"attempt": 1}]


def test_run_conversation_stops_after_protected_mutation_becomes_uncertain(tmp_path):
    home = tmp_path / "profiles" / "emily"
    home.mkdir(parents=True)
    handler_calls = []

    def handler(args, *, execution_runtime, **kwargs):
        handler_calls.append(args)
        execution_runtime.mark_external_mutation_started("issue-create-conversation")
        raise TimeoutError("response not received")

    _register(home, handler)
    context = _context(home)
    tool_call = SimpleNamespace(
        id="call-uncertain",
        type="function",
        function=SimpleNamespace(name=TOOL_NAME, arguments="{}"),
    )
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=None, tool_calls=[tool_call]),
                finish_reason="tool_calls",
            )
        ],
        usage=None,
        model="test-model",
    )
    model_calls = []

    def model_call(_kwargs):
        model_calls.append(True)
        if len(model_calls) > 1:
            raise AssertionError("run_conversation made another model request")
        return response

    try:
        with (
            patch("run_agent.OpenAI", return_value=MagicMock()),
            patch("agent.model_metadata.get_model_context_length", return_value=200000),
        ):
            agent = AIAgent(
                api_key="test-key",
                base_url="https://example.invalid/v1",
                provider="custom",
                model="test-model",
                max_iterations=3,
                enabled_toolsets=["test-capability"],
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
                execution_context=context,
            )

        with (
            patch.object(agent, "_interruptible_api_call", side_effect=model_call),
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch("agent.model_metadata.get_model_context_length", return_value=200000),
        ):
            result = agent.run_conversation("create the issue")
    finally:
        registry.deregister(TOOL_NAME)
        model_tools._clear_tool_defs_cache()

    assert model_calls == [True]
    assert handler_calls == [{}]
    assert result["failed"] is True
    assert result["completed"] is False
    assert result["turn_exit_reason"] == "protected_mutation_uncertain"
    assert "reconciliation required" in result["final_response"].lower()
