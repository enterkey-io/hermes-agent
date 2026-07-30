"""Security contract for scheduler-issued execution capabilities."""

from __future__ import annotations

import copy
import json
import pickle
import threading
import time

import pytest

from agent import execution_capabilities as capabilities
from tools.registry import ToolRegistry


JOB_ID = "8ff7fa2ddb8b"
TOOL_NAME = "bounded_tool"


def _requirement(home, *, job_id=JOB_ID, profile_name="emily"):
    return capabilities.cron_job_capability(
        profile_name=profile_name,
        hermes_home=home,
        job_id=job_id,
    )


def _bound_context(home, *, timeout=1.0):
    job = {"id": JOB_ID}
    permit = capabilities._issue_trusted_cron_dispatch(
        job=job,
        profile_name="emily",
        hermes_home=home,
        execution_id="execution-1",
        allowed_tools={TOOL_NAME},
        protected_handler_timeout_seconds=timeout,
    )
    context = capabilities._issue_cron_job_execution_context(
        permit=permit,
        job=job,
        execution_id="execution-1",
        profile_name="emily",
        hermes_home=home,
    )
    owner = object()
    capabilities._bind_execution_context(context, owner)
    return context, owner


def _registry(home, handler):
    tool_registry = ToolRegistry()
    tool_registry.register(
        name=TOOL_NAME,
        toolset="test",
        schema={
            "name": TOOL_NAME,
            "description": "protected",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=handler,
        execution_capability=_requirement(home),
    )
    return tool_registry


def _dispatch(tool_registry, context, owner):
    grant = capabilities._issue_tool_invocation_grant(
        context,
        owner=owner,
        tool_name=TOOL_NAME,
    )
    return tool_registry.dispatch(
        TOOL_NAME,
        {},
        _execution_capability_grant=grant,
        _execution_capability_owner=owner,
        _execution_context=context,
    )


def test_context_and_grants_are_noncopyable_and_owner_bound(tmp_path):
    context, owner = _bound_context(tmp_path)

    for value in (
        context,
        capabilities._issue_tool_invocation_grant(
            context,
            owner=owner,
            tool_name=TOOL_NAME,
        ),
    ):
        with pytest.raises(TypeError):
            copy.copy(value)
        with pytest.raises(TypeError):
            copy.deepcopy(value)
        with pytest.raises(TypeError):
            pickle.dumps(value)

    with pytest.raises(capabilities.ExecutionCapabilityError):
        capabilities._bind_execution_context(context, object())


def test_runtime_exposes_only_a_shorter_bounded_request_budget(tmp_path):
    context, owner = _bound_context(tmp_path, timeout=0.5)
    grant = capabilities._issue_tool_invocation_grant(
        context,
        owner=owner,
        tool_name=TOOL_NAME,
    )
    runtime = capabilities._consume_tool_invocation_grant(
        grant,
        requirement=_requirement(tmp_path),
        tool_name=TOOL_NAME,
        owner=owner,
        execution_context=context,
    )

    remaining = runtime.remaining_seconds()
    request_timeout = runtime.bounded_timeout(remaining)

    assert 0 < request_timeout < remaining
    runtime._settle()


def test_finalization_stops_cooperative_handler_before_mutation(tmp_path):
    context, owner = _bound_context(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    mutations = []
    dispatch_result = []

    def handler(args, *, execution_runtime, **kwargs):
        entered.set()
        release.wait(timeout=1)
        execution_runtime.check_active()
        mutations.append("mutated")
        return '{"ok": true}'

    tool_registry = _registry(tmp_path, handler)
    worker = threading.Thread(
        target=lambda: dispatch_result.append(
            _dispatch(tool_registry, context, owner)
        )
    )
    worker.start()
    assert entered.wait(timeout=1)

    settlements = []
    finalizer = threading.Thread(
        target=lambda: settlements.append(
                capabilities._finalize_execution_context(
                    context,
                    timeout_seconds=3.0,
            )
        )
    )
    finalizer.start()
    deadline = time.monotonic() + 1
    while capabilities.execution_context_allows(
        context,
        _requirement(tmp_path),
        owner=owner,
    ):
        assert time.monotonic() < deadline
        time.sleep(0.005)
    release.set()
    worker.join(timeout=3)
    finalizer.join(timeout=3)

    assert not worker.is_alive()
    assert not finalizer.is_alive()
    assert mutations == []
    assert settlements == [
        capabilities.ExecutionSettlement(
            settled=True,
            reconciliation_required=False,
            active_invocations=0,
            uncertain_operations=(),
        )
    ]


def test_unknown_external_response_is_terminal_and_requires_reconciliation(
    tmp_path,
):
    context, owner = _bound_context(tmp_path)

    def handler(args, *, execution_runtime, **kwargs):
        execution_runtime.mark_external_mutation_started("issue-create-1")
        raise TimeoutError("response not received")

    result = json.loads(_dispatch(_registry(tmp_path, handler), context, owner))
    settlement = capabilities._finalize_execution_context(
        context,
        timeout_seconds=0.1,
    )

    assert result["error_type"] == "protected_mutation_uncertain"
    assert result["reconciliation_required"] is True
    assert settlement.settled is True
    assert settlement.reconciliation_required is True
    assert settlement.uncertain_operations == ("issue-create-1",)


def test_context_fails_closed_across_process_boundary(tmp_path, monkeypatch):
    context, owner = _bound_context(tmp_path)
    original_pid = capabilities.os.getpid()

    monkeypatch.setattr(capabilities.os, "getpid", lambda: original_pid + 1)

    assert not capabilities.execution_context_allows(
        context,
        _requirement(tmp_path),
        owner=owner,
    )
    with pytest.raises(capabilities.ExecutionCapabilityError):
        capabilities._issue_tool_invocation_grant(
            context,
            owner=owner,
            tool_name=TOOL_NAME,
        )
