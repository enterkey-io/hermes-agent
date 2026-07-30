"""Registry and plugin API tests for execution-capability-gated tools."""

from __future__ import annotations

import json

import pytest

from agent.execution_capabilities import (
    _bind_execution_context,
    _issue_cron_job_execution_context,
    _issue_tool_invocation_grant,
    _issue_trusted_cron_dispatch,
    _revoke_execution_context,
    cron_job_capability,
)
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from tools.registry import ToolRegistry, registry
from tools.tool_search import is_deferrable_tool_name


JOB_ID = "8ff7fa2ddb8b"
TOOL_NAME = "protected"


def _schema(name: str) -> dict:
    return {
        "name": name,
        "description": "A protected test tool",
        "parameters": {"type": "object", "properties": {}},
    }


def _requirement(home, *, job_id=JOB_ID):
    return cron_job_capability(
        profile_name="emily",
        hermes_home=home,
        job_id=job_id,
    )


def _bound_context(home, *, job_id=JOB_ID):
    job = {"id": job_id}
    permit = _issue_trusted_cron_dispatch(
        job=job,
        profile_name="emily",
        hermes_home=home,
        execution_id=f"execution-{job_id}",
        allowed_tools={TOOL_NAME},
        protected_handler_timeout_seconds=1.0,
    )
    context = _issue_cron_job_execution_context(
        permit=permit,
        job=job,
        execution_id=f"execution-{job_id}",
        profile_name="emily",
        hermes_home=home,
    )
    owner = object()
    _bind_execution_context(context, owner)
    return context, owner


def test_protected_schema_requires_exact_live_context_and_owner(tmp_path):
    tool_registry = ToolRegistry()
    requirement = _requirement(tmp_path)
    tool_registry.register(
        name=TOOL_NAME,
        toolset="test",
        schema=_schema(TOOL_NAME),
        handler=lambda args, **kwargs: '{"ok": true}',
        execution_capability=requirement,
    )
    exact_context, exact_owner = _bound_context(tmp_path)
    wrong_context, wrong_owner = _bound_context(tmp_path, job_id="another-job")

    assert tool_registry.get_definitions({TOOL_NAME}) == []
    assert tool_registry.get_definitions(
        {TOOL_NAME},
        execution_context=wrong_context,
        execution_owner=wrong_owner,
    ) == []
    assert tool_registry.get_definitions(
        {TOOL_NAME},
        execution_context=exact_context,
        execution_owner=object(),
    ) == []
    assert [
        item["function"]["name"]
        for item in tool_registry.get_definitions(
            {TOOL_NAME},
            execution_context=exact_context,
            execution_owner=exact_owner,
        )
    ] == [TOOL_NAME]

    _revoke_execution_context(exact_context)
    assert tool_registry.get_definitions(
        {TOOL_NAME},
        execution_context=exact_context,
        execution_owner=exact_owner,
    ) == []


def test_dispatch_rejects_direct_wrong_owner_replay_and_stale_grants(tmp_path):
    tool_registry = ToolRegistry()
    calls = []
    tool_registry.register(
        name=TOOL_NAME,
        toolset="test",
        schema=_schema(TOOL_NAME),
        handler=lambda args, **kwargs: (
            calls.append(kwargs["execution_runtime"])
            or json.dumps({"ok": True})
        ),
        execution_capability=_requirement(tmp_path),
    )

    direct = json.loads(tool_registry.dispatch(TOOL_NAME, {}))
    assert direct["error_type"] == "execution_capability_unavailable"
    assert JOB_ID not in json.dumps(direct)
    assert calls == []

    context, owner = _bound_context(tmp_path)
    wrong_owner_grant = _issue_tool_invocation_grant(
        context,
        owner=owner,
        tool_name=TOOL_NAME,
    )
    wrong_owner = json.loads(
        tool_registry.dispatch(
            TOOL_NAME,
            {},
            _execution_capability_grant=wrong_owner_grant,
            _execution_capability_owner=object(),
            _execution_context=context,
        )
    )
    assert wrong_owner["error_type"] == "execution_capability_unavailable"

    grant = _issue_tool_invocation_grant(
        context,
        owner=owner,
        tool_name=TOOL_NAME,
    )
    dispatch_kwargs = {
        "_execution_capability_grant": grant,
        "_execution_capability_owner": owner,
        "_execution_context": context,
    }
    assert json.loads(
        tool_registry.dispatch(TOOL_NAME, {}, **dispatch_kwargs)
    ) == {"ok": True}
    assert len(calls) == 1

    replay = json.loads(
        tool_registry.dispatch(TOOL_NAME, {}, **dispatch_kwargs)
    )
    assert replay["error_type"] == "execution_capability_unavailable"
    assert len(calls) == 1

    stale_grant = _issue_tool_invocation_grant(
        context,
        owner=owner,
        tool_name=TOOL_NAME,
    )
    _revoke_execution_context(context)
    stale = json.loads(
        tool_registry.dispatch(
            TOOL_NAME,
            {},
            _execution_capability_grant=stale_grant,
            _execution_capability_owner=owner,
            _execution_context=context,
        )
    )
    assert stale["error_type"] == "execution_capability_unavailable"
    assert len(calls) == 1


def test_plugin_context_requires_explicit_profile_home_and_job(tmp_path):
    name = "_test_cron_capability_plugin_tool"
    context = PluginContext(
        PluginManifest(name="test-plugin", source="user"),
        PluginManager(),
    )
    requirement = context.cron_job_capability(
        profile_name="emily",
        hermes_home=tmp_path,
        job_id=JOB_ID,
    )

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


def test_second_plugin_cannot_widen_exact_capability_allowlist(tmp_path):
    tool_registry = ToolRegistry()
    first = PluginContext(
        PluginManifest(
            name="Emily Paperclip Job",
            key="emily-paperclip-job",
            source="user",
        ),
        PluginManager(),
    )
    second = PluginContext(
        PluginManifest(name="Imposter", key="imposter", source="user"),
        PluginManager(),
    )
    first_requirement = first.cron_job_capability(
        profile_name="emily",
        hermes_home=tmp_path,
        job_id=JOB_ID,
    )
    second_requirement = second.cron_job_capability(
        profile_name="emily",
        hermes_home=tmp_path,
        job_id=JOB_ID,
    )

    assert first.registration_owner == "user:emily-paperclip-job"
    assert first_requirement.registration_owner == first.registration_owner
    assert second_requirement.registration_owner != first.registration_owner
    first.manifest.key = "changed-after-context-creation"
    assert first.registration_owner == "user:emily-paperclip-job"

    for name in ("search_issues", "create_backlog_issue"):
        tool_registry.register(
            name=name,
            toolset="paperclip",
            schema=_schema(name),
            handler=lambda args, **kwargs: "{}",
            execution_capability=first_requirement,
            registration_owner=first.registration_owner,
        )

    with pytest.raises(PermissionError, match="already owned"):
        tool_registry.register(
            name="widened_tool",
            toolset="paperclip",
            schema=_schema("widened_tool"),
            handler=lambda args, **kwargs: "{}",
            execution_capability=second_requirement,
            registration_owner=second.registration_owner,
        )

    assert tool_registry.get_execution_capability_tools(first_requirement) == {
        "search_issues",
        "create_backlog_issue",
    }
