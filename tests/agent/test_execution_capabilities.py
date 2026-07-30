"""Security contract for scheduler-issued execution capabilities."""

from __future__ import annotations

import copy
import importlib
import pickle

import pytest


JOB_ID = "8ff7fa2ddb8b"


def _capabilities():
    try:
        return importlib.import_module("agent.execution_capabilities")
    except ModuleNotFoundError:
        pytest.fail(
            "agent.execution_capabilities is required for scheduler-issued "
            "cron job capabilities"
        )


def test_exact_job_requirement_and_live_context_match_only_each_other():
    capabilities = _capabilities()
    requirement = capabilities.cron_job_capability(JOB_ID)
    other_requirement = capabilities.cron_job_capability("another-job")
    context = capabilities._issue_cron_job_execution_context(JOB_ID)
    owner = object()

    capabilities._bind_execution_context(context, owner)

    assert capabilities.execution_context_allows(context, requirement) is True
    assert capabilities.execution_context_allows(context, other_requirement) is False
    assert capabilities.execution_context_fingerprint(context) != ""


def test_execution_context_rejects_copy_deepcopy_and_pickle():
    capabilities = _capabilities()
    context = capabilities._issue_cron_job_execution_context(JOB_ID)

    with pytest.raises(TypeError):
        copy.copy(context)
    with pytest.raises(TypeError):
        copy.deepcopy(context)
    with pytest.raises(TypeError):
        pickle.dumps(context)


def test_execution_context_can_bind_to_only_one_agent_owner():
    capabilities = _capabilities()
    context = capabilities._issue_cron_job_execution_context(JOB_ID)
    first_owner = object()
    nested_owner = object()

    capabilities._bind_execution_context(context, first_owner)

    with pytest.raises(capabilities.ExecutionCapabilityError):
        capabilities._bind_execution_context(context, nested_owner)


def test_tool_invocation_grant_is_exact_one_use_and_not_copyable():
    capabilities = _capabilities()
    requirement = capabilities.cron_job_capability(JOB_ID)
    context = capabilities._issue_cron_job_execution_context(JOB_ID)
    owner = object()
    capabilities._bind_execution_context(context, owner)
    grant = capabilities._issue_tool_invocation_grant(
        context,
        owner=owner,
        tool_name="bounded_tool",
    )

    with pytest.raises(TypeError):
        copy.copy(grant)
    with pytest.raises(TypeError):
        copy.deepcopy(grant)
    with pytest.raises(TypeError):
        pickle.dumps(grant)
    with pytest.raises(capabilities.ExecutionCapabilityError):
        capabilities._consume_tool_invocation_grant(
            grant,
            requirement=requirement,
            tool_name="different_tool",
        )

    runtime = capabilities._consume_tool_invocation_grant(
        grant,
        requirement=requirement,
        tool_name="bounded_tool",
    )
    assert runtime.scoped_state("plugin.test") == {}

    with pytest.raises(capabilities.ExecutionCapabilityError):
        capabilities._consume_tool_invocation_grant(
            grant,
            requirement=requirement,
            tool_name="bounded_tool",
        )


def test_revocation_invalidates_context_grants_and_runtime_state():
    capabilities = _capabilities()
    requirement = capabilities.cron_job_capability(JOB_ID)
    context = capabilities._issue_cron_job_execution_context(JOB_ID)
    owner = object()
    capabilities._bind_execution_context(context, owner)
    grant = capabilities._issue_tool_invocation_grant(
        context,
        owner=owner,
        tool_name="bounded_tool",
    )
    runtime = capabilities._consume_tool_invocation_grant(
        grant,
        requirement=requirement,
        tool_name="bounded_tool",
    )
    state = runtime.scoped_state("plugin.test")
    state["searched"] = True

    capabilities._revoke_execution_context(context)

    assert capabilities.execution_context_allows(context, requirement) is False
    assert capabilities.execution_context_fingerprint(context) == ""
    with pytest.raises(capabilities.ExecutionCapabilityError):
        runtime.scoped_state("plugin.test")
    with pytest.raises(capabilities.ExecutionCapabilityError):
        capabilities._issue_tool_invocation_grant(
            context,
            owner=owner,
            tool_name="bounded_tool",
        )


def test_wrong_job_grant_fails_without_consuming_valid_use():
    capabilities = _capabilities()
    context = capabilities._issue_cron_job_execution_context(JOB_ID)
    owner = object()
    capabilities._bind_execution_context(context, owner)
    grant = capabilities._issue_tool_invocation_grant(
        context,
        owner=owner,
        tool_name="bounded_tool",
    )

    with pytest.raises(capabilities.ExecutionCapabilityError):
        capabilities._consume_tool_invocation_grant(
            grant,
            requirement=capabilities.cron_job_capability("another-job"),
            tool_name="bounded_tool",
        )

    runtime = capabilities._consume_tool_invocation_grant(
        grant,
        requirement=capabilities.cron_job_capability(JOB_ID),
        tool_name="bounded_tool",
    )
    assert runtime.scoped_state("plugin.test") == {}


def test_execution_context_fails_closed_across_process_boundary(monkeypatch):
    capabilities = _capabilities()
    requirement = capabilities.cron_job_capability(JOB_ID)
    context = capabilities._issue_cron_job_execution_context(JOB_ID)
    owner = object()
    capabilities._bind_execution_context(context, owner)
    original_pid = capabilities.os.getpid()

    monkeypatch.setattr(capabilities.os, "getpid", lambda: original_pid + 1)

    assert capabilities.execution_context_allows(context, requirement) is False
    with pytest.raises(capabilities.ExecutionCapabilityError):
        capabilities._issue_tool_invocation_grant(
            context,
            owner=owner,
            tool_name="bounded_tool",
        )
