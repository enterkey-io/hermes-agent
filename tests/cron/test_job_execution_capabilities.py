"""Trusted scheduler issuance and teardown of cron execution capabilities."""

from __future__ import annotations

import os
import concurrent.futures
import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent.execution_capabilities import (
    _bind_execution_context,
    _issue_tool_invocation_grant,
    cron_job_capability,
    execution_context_allows,
)
from cron import scheduler
from tools.registry import registry


JOB_ID = "8ff7fa2ddb8b"
TOOL_NAME = "_test_scheduler_capability_tool"


def _requirement(home: Path, *, profile_name: str = "emily"):
    return cron_job_capability(
        profile_name=profile_name,
        hermes_home=home,
        job_id=JOB_ID,
    )


@pytest.fixture
def protected_tool(tmp_path):
    home = tmp_path / ".hermes" / "profiles" / "emily"
    home.mkdir(parents=True)
    registry.register(
        name=TOOL_NAME,
        toolset="test-capability",
        schema={
            "name": TOOL_NAME,
            "description": "protected",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=lambda args, **kwargs: '{"ok": true}',
        execution_capability=_requirement(home),
    )
    try:
        yield home
    finally:
        registry.deregister(TOOL_NAME)


def test_scheduler_issues_only_for_exact_registered_profile_home_job_and_execution(
    protected_tool,
):
    home = protected_tool
    job = {"id": JOB_ID, "prompt": "trusted"}
    permit = scheduler._issue_registered_cron_dispatch(
        job,
        profile_name="emily",
        hermes_home=home,
        execution_id="execution-1",
    )

    assert permit is not None
    assert scheduler._issue_registered_cron_dispatch(
        dict(job),
        profile_name="maggie",
        hermes_home=home.parent / "maggie",
        execution_id="execution-2",
    ) is None
    assert scheduler._issue_registered_cron_dispatch(
        dict(job, id="other-job"),
        profile_name="emily",
        hermes_home=home,
        execution_id="execution-3",
    ) is None


def test_tick_is_the_only_path_that_attaches_registered_dispatch(protected_tool):
    home = protected_tool
    job = {
        "id": JOB_ID,
        "name": "bounded",
        "prompt": "hello",
        "schedule": {"kind": "cron", "expr": "* * * * *"},
    }
    seen = {}

    def capture(dispatched_job, **kwargs):
        seen["job"] = dispatched_job
        seen["permit"] = kwargs.get("_trusted_dispatch")
        return True

    with (
        concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool,
        patch("cron.scheduler._hermes_home", home),
        patch("cron.scheduler.get_due_jobs", return_value=[job]),
        patch("cron.scheduler.advance_next_runs"),
        patch(
            "cron.scheduler.create_execution",
            return_value={"id": "execution-tick"},
        ),
        patch("cron.scheduler.run_one_job", side_effect=capture),
        patch("cron.scheduler._get_parallel_pool", return_value=pool),
        patch("cron.scheduler._kill_orphaned_mcp_children", create=True),
    ):
        scheduler._running_job_ids.discard(JOB_ID)
        count = scheduler.tick(verbose=False, sync=True)

    assert count == 1
    assert seen["job"]["execution_id"] == "execution-tick"
    assert seen["permit"] is not None


@pytest.mark.parametrize("run_fails", [False, True])
def test_run_job_accepts_only_trusted_dispatch_and_always_finalizes(
    protected_tool,
    run_fails,
):
    home = protected_tool
    job = {
        "id": JOB_ID,
        "name": "bounded",
        "prompt": "hello",
        "execution_id": "execution-1",
    }
    permit = scheduler._issue_registered_cron_dispatch(
        job,
        profile_name="emily",
        hermes_home=home,
        execution_id=job["execution_id"],
    )
    fake_db = MagicMock()
    seen = {}

    class FakeAgent:
        def __init__(self, *args, **kwargs):
            context = kwargs.get("execution_context")
            seen["context"] = context
            seen["constructor_env"] = os.environ.get("HERMES_CRON_JOB_ID")
            _bind_execution_context(context, self)
            seen["owner"] = self
            seen["live_in_constructor"] = execution_context_allows(
                context,
                _requirement(home),
                owner=self,
            )

        def run_conversation(self, prompt):
            seen["run_env"] = os.environ.get("HERMES_CRON_JOB_ID")
            seen["live_in_run"] = execution_context_allows(
                seen["context"],
                _requirement(home),
                owner=self,
            )
            if run_fails:
                raise RuntimeError("test run failure")
            return {"final_response": "ok"}

    with (
        patch("cron.scheduler._hermes_home", home),
        patch("hermes_state.SessionDB", return_value=fake_db),
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value={
                "api_key": "***",
                "base_url": "https://example.invalid/v1",
                "provider": "openrouter",
                "api_mode": "chat_completions",
            },
        ),
        patch("run_agent.AIAgent", FakeAgent),
    ):
        success, _output, final_response, error = scheduler.run_job(
            job,
            _trusted_dispatch=permit,
        )

    assert success is not run_fails
    assert final_response == ("" if run_fails else "ok")
    assert (error is not None) is run_fails
    assert seen["live_in_constructor"] is True
    assert seen["live_in_run"] is True
    assert seen["constructor_env"] is None
    assert seen["run_env"] is None
    assert execution_context_allows(
        seen["context"],
        _requirement(home),
        owner=seen["owner"],
    ) is False


def test_direct_run_and_replayed_or_copied_job_row_fail_closed(protected_tool):
    home = protected_tool
    job = {
        "id": JOB_ID,
        "name": "bounded",
        "prompt": "hello",
        "execution_id": "execution-1",
    }
    permit = scheduler._issue_registered_cron_dispatch(
        job,
        profile_name="emily",
        hermes_home=home,
        execution_id=job["execution_id"],
    )

    success, _output, _response, error = scheduler.run_job(
        dict(job),
        _trusted_dispatch=permit,
    )

    assert success is False
    assert "does not match this job object" in str(error)


def test_scheduler_converts_unknown_mutation_outcome_to_terminal_failure(
    protected_tool,
):
    home = protected_tool
    job = {
        "id": JOB_ID,
        "name": "bounded",
        "prompt": "hello",
        "execution_id": "execution-uncertain",
    }

    def handler(args, *, execution_runtime, **kwargs):
        execution_runtime.mark_external_mutation_started("issue-create-1")
        raise TimeoutError("response not received")

    registry.register(
        name=TOOL_NAME,
        toolset="test-capability",
        schema={
            "name": TOOL_NAME,
            "description": "protected",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=handler,
        execution_capability=_requirement(home),
    )
    permit = scheduler._issue_registered_cron_dispatch(
        job,
        profile_name="emily",
        hermes_home=home,
        execution_id=job["execution_id"],
    )

    class FakeAgent:
        def __init__(self, *args, **kwargs):
            self.execution_context = kwargs["execution_context"]
            _bind_execution_context(self.execution_context, self)

        def run_conversation(self, prompt):
            grant = _issue_tool_invocation_grant(
                self.execution_context,
                owner=self,
                tool_name=TOOL_NAME,
            )
            tool_result = registry.dispatch(
                TOOL_NAME,
                {},
                _execution_capability_grant=grant,
                _execution_capability_owner=self,
                _execution_context=self.execution_context,
            )
            assert json.loads(tool_result)["reconciliation_required"] is True
            return {"final_response": "finished"}

    with (
        patch("cron.scheduler._hermes_home", home),
        patch("hermes_state.SessionDB", return_value=MagicMock()),
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value={
                "api_key": "***",
                "base_url": "https://example.invalid/v1",
                "provider": "openrouter",
                "api_mode": "chat_completions",
            },
        ),
        patch("run_agent.AIAgent", FakeAgent),
    ):
        success, _output, final_response, error = scheduler.run_job(
            job,
            _trusted_dispatch=permit,
        )

    assert success is False
    assert final_response == ""
    assert "reconciliation required" in str(error).lower()


def test_scheduler_waits_for_protected_handler_settlement_on_timeout(
    protected_tool,
    monkeypatch,
):
    home = protected_tool
    job = {
        "id": JOB_ID,
        "name": "bounded",
        "prompt": "hello",
        "execution_id": "execution-timeout",
    }
    entered = threading.Event()
    done = threading.Event()

    def handler(args, *, execution_runtime, **kwargs):
        entered.set()
        try:
            while True:
                time.sleep(0.01)
                execution_runtime.check_active()
        finally:
            time.sleep(0.2)
            done.set()

    registry.register(
        name=TOOL_NAME,
        toolset="test-capability",
        schema={
            "name": TOOL_NAME,
            "description": "protected",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=handler,
        execution_capability=_requirement(home),
    )
    permit = scheduler._issue_registered_cron_dispatch(
        job,
        profile_name="emily",
        hermes_home=home,
        execution_id=job["execution_id"],
    )

    class FakeAgent:
        def __init__(self, *args, **kwargs):
            self.execution_context = kwargs["execution_context"]
            _bind_execution_context(self.execution_context, self)

        def run_conversation(self, prompt):
            grant = _issue_tool_invocation_grant(
                self.execution_context,
                owner=self,
                tool_name=TOOL_NAME,
            )
            registry.dispatch(
                TOOL_NAME,
                {},
                _execution_capability_grant=grant,
                _execution_capability_owner=self,
                _execution_context=self.execution_context,
            )
            return {"final_response": "unexpected"}

        def get_activity_summary(self):
            return {"seconds_since_activity": 10, "last_activity_desc": "tool"}

        def interrupt(self, reason):
            pass

    real_wait = concurrent.futures.wait

    def fast_wait(fs, timeout=None, **kwargs):
        return real_wait(fs, timeout=min(float(timeout or 0.05), 0.05), **kwargs)

    monkeypatch.setenv("HERMES_CRON_TIMEOUT", "0.01")
    monkeypatch.setattr(scheduler.concurrent.futures, "wait", fast_wait)
    with (
        patch("cron.scheduler._hermes_home", home),
        patch("hermes_state.SessionDB", return_value=MagicMock()),
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value={
                "api_key": "***",
                "base_url": "https://example.invalid/v1",
                "provider": "openrouter",
                "api_mode": "chat_completions",
            },
        ),
        patch("run_agent.AIAgent", FakeAgent),
    ):
        success, _output, _response, error = scheduler.run_job(
            job,
            _trusted_dispatch=permit,
        )

    assert entered.is_set()
    assert done.is_set()
    assert success is False
    assert "timeouterror" in str(error).lower()
