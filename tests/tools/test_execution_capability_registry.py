"""Registry and plugin API tests for execution-capability-gated tools."""

from __future__ import annotations

import json

from agent.execution_capabilities import (
    _bind_execution_context,
    _issue_cron_job_execution_context,
    _issue_tool_invocation_grant,
    _revoke_execution_context,
    cron_job_capability,
)
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from tools.registry import ToolRegistry, registry
from tools.tool_search import is_deferrable_tool_name


JOB_ID = "8ff7fa2ddb8b"


def _schema(name: str) -> dict:
    return {
        "name": name,
        "description": "A protected test tool",
        "parameters": {"type": "object", "properties": {}},
    }


def _bound_context(job_id: str = JOB_ID):
    context = _issue_cron_job_execution_context(job_id)
    owner = object()
    _bind_execution_context(context, owner)
    return context, owner


def test_protected_schema_requires_exact_live_execution_context():
    tool_registry = ToolRegistry()
    requirement = cron_job_capability(JOB_ID)
    tool_registry.register(
        name="protected",
        toolset="test",
        schema=_schema("protected"),
        handler=lambda args, **kwargs: '{"ok": true}',
        execution_capability=requirement,
    )
    exact_context, _ = _bound_context()
    wrong_context, _ = _bound_context("another-job")

    assert tool_registry.get_definitions({"protected"}) == []
    assert tool_registry.get_definitions(
        {"protected"}, execution_context=wrong_context
    ) == []
    assert [
        item["function"]["name"]
        for item in tool_registry.get_definitions(
            {"protected"}, execution_context=exact_context
        )
    ] == ["protected"]

    _revoke_execution_context(exact_context)
    assert tool_registry.get_definitions(
        {"protected"}, execution_context=exact_context
    ) == []


def test_protected_dispatch_rejects_direct_wrong_job_replay_and_stale_grants():
    tool_registry = ToolRegistry()
    calls = []
    requirement = cron_job_capability(JOB_ID)
    tool_registry.register(
        name="protected",
        toolset="test",
        schema=_schema("protected"),
        handler=lambda args, **kwargs: (
            calls.append(kwargs["execution_runtime"])
            or json.dumps({"ok": True})
        ),
        execution_capability=requirement,
    )

    direct = json.loads(tool_registry.dispatch("protected", {}))
    assert direct["error_type"] == "execution_capability_unavailable"
    assert JOB_ID not in json.dumps(direct)
    assert calls == []

    wrong_context, wrong_owner = _bound_context("another-job")
    wrong_grant = _issue_tool_invocation_grant(
        wrong_context,
        owner=wrong_owner,
        tool_name="protected",
    )
    wrong = json.loads(
        tool_registry.dispatch(
            "protected",
            {},
            _execution_capability_grant=wrong_grant,
        )
    )
    assert wrong["error_type"] == "execution_capability_unavailable"
    assert calls == []

    context, owner = _bound_context()
    grant = _issue_tool_invocation_grant(
        context,
        owner=owner,
        tool_name="protected",
    )
    assert json.loads(
        tool_registry.dispatch(
            "protected",
            {},
            _execution_capability_grant=grant,
        )
    ) == {"ok": True}
    assert len(calls) == 1

    replay = json.loads(
        tool_registry.dispatch(
            "protected",
            {},
            _execution_capability_grant=grant,
        )
    )
    assert replay["error_type"] == "execution_capability_unavailable"
    assert len(calls) == 1

    stale_grant = _issue_tool_invocation_grant(
        context,
        owner=owner,
        tool_name="protected",
    )
    _revoke_execution_context(context)
    stale = json.loads(
        tool_registry.dispatch(
            "protected",
            {},
            _execution_capability_grant=stale_grant,
        )
    )
    assert stale["error_type"] == "execution_capability_unavailable"
    assert len(calls) == 1


def test_plugin_context_declares_capability_and_registers_protected_tool():
    name = "_test_cron_capability_plugin_tool"
    context = PluginContext(
        PluginManifest(name="test-plugin", source="user"),
        PluginManager(),
    )
    requirement = context.cron_job_capability(JOB_ID)

    context.register_tool(
        name=name,
        toolset="test-plugin",
        schema=_schema(name),
        handler=lambda args, **kwargs: '{"ok": true}',
        execution_capability=requirement,
    )
    try:
        entry = registry.get_entry(name)
        assert entry is not None
        assert entry.execution_capability == requirement
        assert is_deferrable_tool_name(name) is False
    finally:
        registry.deregister(name)
