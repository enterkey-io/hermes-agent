"""Agent-path tests for protected tool schema and invocation grants."""

from __future__ import annotations

import json

import model_tools
from agent.agent_runtime_helpers import invoke_tool
from agent.execution_capabilities import (
    _bind_execution_context,
    _issue_cron_job_execution_context,
    cron_job_capability,
)
from tools.registry import registry


JOB_ID = "8ff7fa2ddb8b"
TOOL_NAME = "_test_agent_path_protected_tool"


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


def test_model_tool_definitions_filter_protected_tool_by_execution_context():
    registry.register(
        name=TOOL_NAME,
        toolset="test-capability",
        schema={
            "name": TOOL_NAME,
            "description": "Protected",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=lambda args, **kwargs: '{"ok": true}',
        execution_capability=cron_job_capability(JOB_ID),
    )
    exact_context = _issue_cron_job_execution_context(JOB_ID)
    exact_owner = object()
    _bind_execution_context(exact_context, exact_owner)
    try:
        absent = model_tools.get_tool_definitions(
            enabled_toolsets=["test-capability"],
            quiet_mode=True,
        )
        present = model_tools.get_tool_definitions(
            enabled_toolsets=["test-capability"],
            quiet_mode=True,
            execution_context=exact_context,
        )
    finally:
        registry.deregister(TOOL_NAME)
        model_tools._clear_tool_defs_cache()

    assert TOOL_NAME not in {item["function"]["name"] for item in absent}
    assert TOOL_NAME in {item["function"]["name"] for item in present}


def test_real_agent_helper_mints_grant_but_direct_model_dispatch_fails():
    calls = []
    registry.register(
        name=TOOL_NAME,
        toolset="test-capability",
        schema={
            "name": TOOL_NAME,
            "description": "Protected",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=lambda args, **kwargs: (
            calls.append(kwargs["execution_runtime"])
            or json.dumps({"ok": True})
        ),
        execution_capability=cron_job_capability(JOB_ID),
    )
    context = _issue_cron_job_execution_context(JOB_ID)
    agent = _Agent(context)
    _bind_execution_context(context, agent)
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
    assert result == {"ok": True}
    assert len(calls) == 1


def test_agent_without_execution_context_cannot_mint_protected_grant():
    registry.register(
        name=TOOL_NAME,
        toolset="test-capability",
        schema={
            "name": TOOL_NAME,
            "description": "Protected",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=lambda args, **kwargs: '{"ok": true}',
        execution_capability=cron_job_capability(JOB_ID),
    )
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
