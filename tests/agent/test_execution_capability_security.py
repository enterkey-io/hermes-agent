"""Adversarial tests for trusted cron execution capabilities."""

from __future__ import annotations

import contextvars
import copy
import json
import threading
import uuid
from pathlib import Path

import pytest

from agent import execution_capabilities as capabilities
from tools.registry import ToolRegistry


JOB_ID = "8ff7fa2ddb8b"
EXECUTION_ID = "77777777-7777-4777-8777-777777777777"
TOOL_NAME = "bounded_tool"


def _requirement(home: Path):
    return capabilities.cron_job_capability(
        profile_name="emily",
        hermes_home=home,
        job_id=JOB_ID,
    )


def _trusted_context(home: Path):
    job = {"id": JOB_ID, "prompt": "trusted"}
    permit = capabilities._issue_trusted_cron_dispatch(
        job=job,
        profile_name="emily",
        hermes_home=home,
        execution_id=EXECUTION_ID,
        allowed_tools={TOOL_NAME},
        protected_handler_timeout_seconds=1.0,
    )
    context = capabilities._issue_cron_job_execution_context(
        permit=permit,
        job=job,
        execution_id=EXECUTION_ID,
        profile_name="emily",
        hermes_home=home,
    )
    owner = object()
    capabilities._bind_execution_context(context, owner)
    return job, context, owner


def test_dispatch_permit_binds_profile_home_job_execution_allowlist_and_row_identity(
    tmp_path,
):
    home = tmp_path / ".hermes" / "profiles" / "emily"
    home.mkdir(parents=True)
    job = {"id": JOB_ID, "prompt": "trusted"}
    permit = capabilities._issue_trusted_cron_dispatch(
        job=job,
        profile_name="emily",
        hermes_home=home,
        execution_id=EXECUTION_ID,
        allowed_tools={TOOL_NAME},
        protected_handler_timeout_seconds=1.0,
    )

    with pytest.raises(capabilities.ExecutionCapabilityError):
        capabilities._issue_cron_job_execution_context(
            permit=permit,
            job=job,
            execution_id=EXECUTION_ID,
            profile_name="maggie",
            hermes_home=tmp_path / ".hermes" / "profiles" / "maggie",
        )

    with pytest.raises(capabilities.ExecutionCapabilityError):
        capabilities._issue_cron_job_execution_context(
            permit=permit,
            job=dict(job),
            execution_id=EXECUTION_ID,
            profile_name="emily",
            hermes_home=home,
        )

    context = capabilities._issue_cron_job_execution_context(
        permit=permit,
        job=job,
        execution_id=EXECUTION_ID,
        profile_name="emily",
        hermes_home=home,
    )
    owner = object()
    capabilities._bind_execution_context(context, owner)

    assert capabilities.execution_context_allows(
        context,
        _requirement(home),
        owner=owner,
    )
    assert not capabilities.execution_context_allows(
        context,
        capabilities.cron_job_capability(
            profile_name="maggie",
            hermes_home=tmp_path / ".hermes" / "profiles" / "maggie",
            job_id=JOB_ID,
        ),
        owner=owner,
    )
    with pytest.raises(capabilities.ExecutionCapabilityError):
        capabilities._issue_tool_invocation_grant(
            context,
            owner=owner,
            tool_name="not-allowed",
        )


def test_dispatch_permit_is_noncopyable_and_one_use(tmp_path):
    home = tmp_path / ".hermes" / "profiles" / "emily"
    home.mkdir(parents=True)
    job = {"id": JOB_ID}
    permit = capabilities._issue_trusted_cron_dispatch(
        job=job,
        profile_name="emily",
        hermes_home=home,
        execution_id=EXECUTION_ID,
        allowed_tools={TOOL_NAME},
        protected_handler_timeout_seconds=1.0,
    )

    with pytest.raises(TypeError):
        copy.copy(permit)

    context = capabilities._issue_cron_job_execution_context(
        permit=permit,
        job=job,
        execution_id=EXECUTION_ID,
        profile_name="emily",
        hermes_home=home,
    )
    assert context is not None
    with pytest.raises(capabilities.ExecutionCapabilityError):
        capabilities._issue_cron_job_execution_context(
            permit=permit,
            job=job,
            execution_id=EXECUTION_ID,
            profile_name="emily",
            hermes_home=home,
        )


def test_grant_requires_exact_owner_and_context_without_consuming_valid_use(tmp_path):
    home = tmp_path / ".hermes" / "profiles" / "emily"
    home.mkdir(parents=True)
    _job, context, owner = _trusted_context(home)
    grant = capabilities._issue_tool_invocation_grant(
        context,
        owner=owner,
        tool_name=TOOL_NAME,
    )
    _other_job, other_context, other_owner = _trusted_context(home)
    requirement = _requirement(home)

    with pytest.raises(capabilities.ExecutionCapabilityError):
        capabilities._consume_tool_invocation_grant(
            grant,
            requirement=requirement,
            tool_name=TOOL_NAME,
            owner=other_owner,
            execution_context=context,
        )
    with pytest.raises(capabilities.ExecutionCapabilityError):
        capabilities._consume_tool_invocation_grant(
            grant,
            requirement=requirement,
            tool_name=TOOL_NAME,
            owner=owner,
            execution_context=other_context,
        )

    runtime = capabilities._consume_tool_invocation_grant(
        grant,
        requirement=requirement,
        tool_name=TOOL_NAME,
        owner=owner,
        execution_context=context,
    )
    assert runtime.remaining_seconds() > 0


def test_concurrent_double_consume_accepts_exactly_one(tmp_path):
    home = tmp_path / ".hermes" / "profiles" / "emily"
    home.mkdir(parents=True)
    _job, context, owner = _trusted_context(home)
    grant = capabilities._issue_tool_invocation_grant(
        context,
        owner=owner,
        tool_name=TOOL_NAME,
    )
    barrier = threading.Barrier(2)
    outcomes = []

    def consume():
        barrier.wait(timeout=2)
        try:
            capabilities._consume_tool_invocation_grant(
                grant,
                requirement=_requirement(home),
                tool_name=TOOL_NAME,
                owner=owner,
                execution_context=context,
            )
        except capabilities.ExecutionCapabilityError:
            outcomes.append("denied")
        else:
            outcomes.append("accepted")

    threads = [threading.Thread(target=consume) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert sorted(outcomes) == ["accepted", "denied"]


def test_copied_contextvar_cannot_authorize_schema_or_dispatch(tmp_path):
    home = tmp_path / ".hermes" / "profiles" / "emily"
    home.mkdir(parents=True)
    _job, execution_context, owner = _trusted_context(home)
    leaked_context = contextvars.ContextVar("leaked_execution_context")
    leaked_context.set(execution_context)
    copied = contextvars.copy_context()
    registry = ToolRegistry()
    registry.register(
        name=TOOL_NAME,
        toolset="test",
        schema={
            "name": TOOL_NAME,
            "description": "protected",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=lambda args, **kwargs: json.dumps({"ok": True}),
        execution_capability=_requirement(home),
    )

    copied_context = copied.run(leaked_context.get)
    assert registry.get_definitions(
        {TOOL_NAME},
        execution_context=copied_context,
    ) == []

    grant = capabilities._issue_tool_invocation_grant(
        execution_context,
        owner=owner,
        tool_name=TOOL_NAME,
    )
    result = json.loads(
        registry.dispatch(
            TOOL_NAME,
            {},
            _execution_capability_grant=grant,
            _execution_capability_owner=object(),
            _execution_context=copied_context,
        )
    )
    assert result["error_type"] == "execution_capability_unavailable"
