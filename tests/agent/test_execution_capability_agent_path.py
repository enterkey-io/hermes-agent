"""Agent-path tests for protected tool schema and invocation grants."""

from __future__ import annotations

import json
import types

import model_tools
from agent.agent_runtime_helpers import invoke_tool
from agent.execution_capabilities import (
    _bind_execution_context,
    _issue_cron_job_execution_context,
    _issue_trusted_cron_dispatch,
    cron_job_capability,
)
from tools import mcp_tool
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
