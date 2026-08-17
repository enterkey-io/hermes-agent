"""Trusted scheduler issuance and teardown of cron execution capabilities."""

from __future__ import annotations

import os
import concurrent.futures
import json
import threading
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent.execution_capabilities import (
    ExecutionCapabilityError,
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


def test_trusted_scheduler_tick_attaches_registered_dispatch(protected_tool):
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
        patch(
            "cron.scheduler.claim_job_for_fire",
            return_value={**job, "fire_claim": {"by": "owner-tick"}},
        ),
        patch("cron.scheduler.run_one_job", side_effect=capture),
        patch("cron.scheduler._get_parallel_pool", return_value=pool),
        patch("cron.scheduler._kill_orphaned_mcp_children", create=True),
    ):
        scheduler._running_job_ids.discard(JOB_ID)
        trusted_tick = scheduler._take_trusted_gateway_tick()
        count = trusted_tick(verbose=False, sync=True)

    assert count == 1
    assert seen["job"]["execution_id"] == "execution-tick"
    assert seen["permit"] is not None


def test_private_tick_impl_does_not_accept_forged_scheduler_provenance(
    protected_tool,
):
    job = {
        "id": JOB_ID,
        "name": "bounded",
        "schedule": {"kind": "cron", "expr": "* * * * *"},
    }
    events = []

    with (
        concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool,
        patch("cron.scheduler._hermes_home", protected_tool),
        patch("cron.scheduler.get_due_jobs", return_value=[job]),
            patch("cron.scheduler.advance_next_runs"),
        patch(
            "cron.scheduler.create_execution",
            return_value={"id": "execution-forged"},
        ),
        patch("cron.scheduler.claim_job_for_fire", return_value=job),
        patch(
            "cron.scheduler._issue_registered_cron_dispatch",
            side_effect=lambda *_args, **_kwargs: events.append("issue"),
        ),
        patch(
            "cron.scheduler.run_one_job",
            side_effect=lambda *_args, **_kwargs: events.append("run") or True,
        ),
        patch("cron.scheduler._get_parallel_pool", return_value=pool),
    ):
        scheduler._running_job_ids.discard(JOB_ID)
        assert scheduler._tick_impl(
            verbose=False,
            sync=True,
            _scheduler_provenance=object(),
        ) == 1

    assert events == ["run"]


def test_public_tick_filters_protected_profile_before_job_state_mutation(
    protected_tool,
    monkeypatch,
):
    events = []
    monkeypatch.setattr(scheduler, "_hermes_home", protected_tool)
    monkeypatch.setattr(
        scheduler,
        "_get_lock_paths",
        lambda: events.append("lock")
        or (protected_tool / "cron", protected_tool / "cron" / ".tick.lock"),
    )
    monkeypatch.setattr(
        scheduler,
        "get_due_jobs",
        lambda *, exclude_job_ids=None: events.append(
            ("read-schedules", exclude_job_ids)
        )
        or [],
    )
    monkeypatch.setattr(
        scheduler,
        "advance_next_runs",
        lambda _job_ids: events.append("advance-schedule"),
    )
    monkeypatch.setattr(
        scheduler,
        "create_execution",
        lambda *_args, **_kwargs: events.append("create-execution"),
    )
    monkeypatch.setattr(
        scheduler,
        "_issue_registered_cron_dispatch",
        lambda *_args, **_kwargs: events.append("issue"),
    )
    monkeypatch.setattr(
        scheduler, "run_one_job", lambda *_args, **_kwargs: events.append("handler")
    )

    assert scheduler.tick(verbose=False, sync=True) == 0

    assert events == ["lock", ("read-schedules", {JOB_ID})]


def test_public_tick_runs_ordinary_due_job_without_mutating_protected_sibling(
    protected_tool,
    monkeypatch,
):
    ordinary = {
        "id": "ordinary-job",
        "name": "ordinary",
        "schedule": {"kind": "cron", "expr": "* * * * *"},
    }
    protected = {
        "id": JOB_ID,
        "name": "protected",
        "schedule": {"kind": "cron", "expr": "* * * * *"},
    }
    events = []

    monkeypatch.setattr(scheduler, "_hermes_home", protected_tool)

    def due_jobs(*, exclude_job_ids=None):
        assert exclude_job_ids == {JOB_ID}
        events.append(("read", frozenset(exclude_job_ids)))
        return [ordinary]

    monkeypatch.setattr(scheduler, "get_due_jobs", due_jobs)
    monkeypatch.setattr(
        scheduler,
        "advance_next_runs",
        lambda job_ids: events.append(("advance", tuple(job_ids))),
    )
    monkeypatch.setattr(
        scheduler,
        "create_execution",
        lambda job_id, **_kwargs: events.append(("create", job_id))
        or {"id": "execution-ordinary"},
    )
    monkeypatch.setattr(
        scheduler,
        "claim_job_for_fire",
        lambda job_id, **_kwargs: ordinary if job_id == ordinary["id"] else None,
    )
    monkeypatch.setattr(
        scheduler,
        "run_one_job",
        lambda job, **_kwargs: events.append(("run", job["id"])) or True,
    )

    scheduler._running_job_ids.discard(ordinary["id"])
    assert scheduler.tick(verbose=False, sync=True) == 1
    assert ("advance", JOB_ID) not in events
    assert ("create", JOB_ID) not in events
    assert ("run", JOB_ID) not in events
    assert events == [
        ("read", frozenset({JOB_ID})),
        ("advance", ("ordinary-job",)),
        ("create", "ordinary-job"),
        ("run", "ordinary-job"),
    ]


@pytest.mark.parametrize(
    "protected_run_at",
    [
        "2026-07-29T10:00:00+00:00",
        "2026-07-29T11:00:00+00:00",
    ],
)
def test_due_scan_does_not_repair_claim_or_advance_excluded_protected_job(
    monkeypatch,
    protected_run_at,
):
    from cron import jobs

    protected = {
        "id": JOB_ID,
        "enabled": True,
        "schedule": {"kind": "once", "run_at": protected_run_at},
        "next_run_at": protected_run_at,
    }
    ordinary = {
        "id": "ordinary-job",
        "enabled": True,
        "schedule": {"kind": "once", "run_at": "2026-07-29T10:00:00+00:00"},
        "next_run_at": "2026-07-29T10:00:00+00:00",
    }
    stored = [dict(protected), dict(ordinary)]
    saved = []
    monkeypatch.setattr(jobs, "load_jobs", lambda: stored)
    monkeypatch.setattr(
        jobs,
        "save_jobs",
        lambda value, **_kwargs: saved.append(value),
    )
    monkeypatch.setattr(
        jobs,
        "_hermes_now",
        lambda: datetime.fromisoformat("2026-07-29T10:01:00+00:00"),
    )

    due = jobs._get_due_jobs_locked(exclude_job_ids={JOB_ID})

    assert [job["id"] for job in due] == ["ordinary-job"]
    assert stored[0] == protected
    assert "run_claim" in stored[1]
    assert saved


def test_trigger_protected_job_rejects_before_schedule_mutation(
    protected_tool,
    monkeypatch,
):
    from cron import jobs

    events = []
    monkeypatch.setattr(scheduler, "_hermes_home", protected_tool)
    monkeypatch.setattr(
        jobs,
        "resolve_job_ref",
        lambda job_id: events.append(("resolve", job_id)) or {"id": JOB_ID},
    )
    monkeypatch.setattr(
        jobs,
        "update_job",
        lambda *_args, **_kwargs: events.append(("update",)) or {},
    )

    with pytest.raises(ExecutionCapabilityError, match="protected"):
        jobs.trigger_job(JOB_ID)

    assert events == [("resolve", JOB_ID)]


def test_forged_dispatch_is_rejected_before_no_agent_or_prerun_side_effects(
    protected_tool,
    monkeypatch,
):
    events = []
    monkeypatch.setattr(scheduler, "_hermes_home", protected_tool)
    monkeypatch.setattr(
        scheduler,
        "_run_job_script_with_claim_heartbeat",
        lambda *_args, **_kwargs: events.append("script") or (True, ""),
    )

    result = scheduler.run_job(
        {
            "id": JOB_ID,
            "name": "protected",
            "execution_id": "execution-forged",
            "no_agent": True,
            "script": "/must-not-run",
        },
        _trusted_dispatch=object(),
    )

    assert result[0] is False
    assert "trusted cron dispatch" in str(result[3]).lower()
    assert events == []


def test_forged_dispatch_is_rejected_before_session_db_and_wake_gate_script(
    protected_tool,
    monkeypatch,
):
    events = []
    monkeypatch.setattr(scheduler, "_hermes_home", protected_tool)
    monkeypatch.setattr(
        scheduler,
        "_run_job_script_with_claim_heartbeat",
        lambda *_args, **_kwargs: events.append("script")
        or (True, '{"wakeAgent": false}'),
    )
    monkeypatch.setattr(
        "hermes_state.SessionDB",
        lambda: events.append("session-db"),
    )

    result = scheduler.run_job(
        {
            "id": JOB_ID,
            "name": "protected",
            "execution_id": "execution-forged",
            "script": "/must-not-run",
        },
        _trusted_dispatch=object(),
    )

    assert result[0] is False
    assert "trusted cron dispatch" in str(result[3]).lower()
    assert events == []


def test_internal_runner_requires_one_use_opaque_admission_before_side_effects(
    protected_tool,
    monkeypatch,
):
    events = []
    monkeypatch.setattr(scheduler, "_hermes_home", protected_tool)
    monkeypatch.setattr(
        scheduler,
        "_run_job_script_with_claim_heartbeat",
        lambda *_args, **_kwargs: events.append("script") or (True, "ran"),
    )
    job = {
        "id": JOB_ID,
        "name": "protected",
        "no_agent": True,
        "script": "/must-not-run",
    }

    with pytest.raises(ExecutionCapabilityError, match="admission"):
        scheduler._run_job_after_admission(job)

    assert events == []


def test_direct_run_one_job_rejects_before_execution_row_or_dispatch_claim(
    protected_tool,
    monkeypatch,
):
    events = []
    monkeypatch.setattr(scheduler, "_hermes_home", protected_tool)
    monkeypatch.setattr(
        scheduler,
        "create_execution",
        lambda *_args, **_kwargs: events.append("execution"),
    )
    monkeypatch.setattr(
        scheduler,
        "claim_dispatch",
        lambda *_args, **_kwargs: events.append("claim") or True,
    )
    monkeypatch.setattr(
        scheduler,
        "run_job",
        lambda *_args, **_kwargs: events.append("handler"),
    )

    assert scheduler.run_one_job({"id": JOB_ID, "name": "protected"}) is False
    assert events == []


def test_public_fire_denies_exact_protected_job_but_runs_ordinary_sibling(
    protected_tool,
    monkeypatch,
):
    from cron.scheduler_provider import InProcessCronScheduler

    events = []
    monkeypatch.setattr(scheduler, "_hermes_home", protected_tool)
    monkeypatch.setattr(
        "cron.jobs.claim_job_for_fire",
        lambda job_id, **_kwargs: events.append(("claim", job_id))
        or {"id": job_id, "name": "manual"},
    )
    monkeypatch.setattr(
        "cron.jobs.get_job",
        lambda job_id: {"id": job_id, "name": "manual"},
    )
    monkeypatch.setattr(
        "cron.executions.create_execution",
        lambda job_id, **_kwargs: {"id": f"execution-{job_id}"},
    )
    monkeypatch.setattr(
        scheduler,
        "_issue_registered_cron_dispatch",
        lambda *_args, **_kwargs: events.append(("issue",)),
    )
    monkeypatch.setattr(
        scheduler,
        "run_one_job",
        lambda job, **kwargs: events.append(
            ("run", job["id"], kwargs.get("_trusted_dispatch"))
        )
        or True,
    )

    provider = InProcessCronScheduler()
    assert provider.fire_due(JOB_ID) is False
    assert events == []

    assert provider.fire_due("ordinary-job") is True
    assert events == [
        ("claim", "ordinary-job"),
        ("run", "ordinary-job", None),
    ]


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
    assert "does not match" in str(error)


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


def _patch_run_one_job_pipeline(monkeypatch, *, run_result, events):
    monkeypatch.setattr(scheduler, "claim_dispatch", lambda _job_id: True)
    monkeypatch.setattr(
        scheduler,
        "mark_execution_running",
        lambda execution_id: events.append(("running", execution_id)),
    )

    def fake_run_job(job, *, defer_agent_teardown=None, **_kwargs):
        events.append(("run", job["id"]))
        return run_result

    monkeypatch.setattr(scheduler, "run_job", fake_run_job)
    monkeypatch.setattr(
        scheduler,
        "save_job_output",
        lambda job_id, output: events.append(("save", job_id)) or "/tmp/output",
    )
    monkeypatch.setattr(
        scheduler,
        "_deliver_result",
        lambda job, content, **kwargs: events.append(("deliver", job["id"])),
    )
    monkeypatch.setattr(
        scheduler,
        "mark_job_run",
        lambda job_id, success, error=None, **kwargs: events.append(
            ("mark", job_id, success, error)
        ),
    )

    def finish(execution_id, *, success, error=None, **kwargs):
        events.append(("persist", execution_id, success, error))
        return {
            "id": execution_id,
            "status": "completed" if success else "failed",
            "error": error,
        }

    monkeypatch.setattr(scheduler, "finish_execution", finish)


def test_uncooperative_protected_handler_persists_then_fail_stops_once(
    monkeypatch,
    caplog,
):
    events = []
    reconciliation_key = "issue-create-persist-before-exit"
    failure = scheduler.ProtectedMutationFailure(
        reconciliation_keys=(reconciliation_key,),
        uncooperative=True,
    )
    _patch_run_one_job_pipeline(
        monkeypatch,
        run_result=(False, "failed output", "", failure),
        events=events,
    )
    deferred_agent = object()

    def run_with_deferred_agent(job, *, defer_agent_teardown=None, **_kwargs):
        events.append(("run", job["id"]))
        defer_agent_teardown.append(deferred_agent)
        return False, "failed output", "", failure

    monkeypatch.setattr(scheduler, "run_job", run_with_deferred_agent)
    monkeypatch.setattr(
        scheduler,
        "_teardown_cron_agent",
        lambda agent, job_id: events.append(("teardown", job_id, agent)),
    )
    monkeypatch.setattr(scheduler, "_protected_fail_stop", threading.Event())

    def fatal_restart(recorded_failure):
        persisted = [event for event in events if event[0] == "persist"]
        assert persisted
        assert reconciliation_key in persisted[-1][3]
        assert scheduler._protected_fail_stop.is_set()
        assert recorded_failure is failure
        assert "Fail-stopping the gateway" in caplog.text
        assert events[-1] == ("teardown", JOB_ID, deferred_agent)
        events.append(("fatal_restart", str(recorded_failure)))

    job = {
        "id": JOB_ID,
        "name": "bounded",
        "execution_id": "execution-uncooperative",
    }
    with pytest.raises(scheduler.GatewayFailStopRequired):
        scheduler.run_one_job(job, _fatal_restart_hook=fatal_restart)

    with pytest.raises(scheduler.GatewayFailStopRequired):
        scheduler.run_one_job(job, _fatal_restart_hook=fatal_restart)

    event_names = [event[0] for event in events]
    assert event_names == ["running", "run", "persist", "teardown", "fatal_restart"]
    assert reconciliation_key not in caplog.text


def test_uncooperative_persistence_failure_still_tears_down_before_raising(
    monkeypatch,
):
    events = []
    failure = scheduler.ProtectedMutationFailure(
        reconciliation_keys=("issue-create-persistence-uncertain",),
        uncooperative=True,
    )
    deferred_agent = object()
    monkeypatch.setattr(scheduler, "claim_dispatch", lambda _job_id: True)
    monkeypatch.setattr(scheduler, "mark_execution_running", lambda _id: None)

    def run_with_deferred_agent(job, *, defer_agent_teardown=None, **_kwargs):
        defer_agent_teardown.append(deferred_agent)
        events.append("run")
        return False, "", "", failure

    monkeypatch.setattr(scheduler, "run_job", run_with_deferred_agent)
    monkeypatch.setattr(
        scheduler,
        "finish_execution",
        lambda *_args, **_kwargs: events.append("persist-unconfirmed") or None,
    )
    monkeypatch.setattr(
        scheduler,
        "_teardown_cron_agent",
        lambda agent, _job_id: events.append(("teardown", agent)),
    )
    monkeypatch.setattr(scheduler, "_protected_fail_stop", threading.Event())

    with pytest.raises(scheduler.GatewayFailStopRequired):
        scheduler.run_one_job(
            {"id": JOB_ID, "execution_id": "execution-persist-uncertain"},
            _fatal_restart_hook=lambda _failure: events.append("fatal"),
        )

    assert events == [
        "run",
        "persist-unconfirmed",
        ("teardown", deferred_agent),
    ]
    assert scheduler._protected_fail_stop.is_set()


def test_uncooperative_non_systemd_failure_tears_down_before_fatal_error(
    monkeypatch,
):
    events = []
    failure = scheduler.ProtectedMutationFailure(
        reconciliation_keys=("issue-create-cli",),
        uncooperative=True,
    )
    deferred_agent = object()
    _patch_run_one_job_pipeline(
        monkeypatch,
        run_result=(False, "", "", failure),
        events=events,
    )

    def run_with_deferred_agent(job, *, defer_agent_teardown=None, **_kwargs):
        defer_agent_teardown.append(deferred_agent)
        events.append(("run", job["id"]))
        return False, "", "", failure

    monkeypatch.setattr(scheduler, "run_job", run_with_deferred_agent)
    monkeypatch.setattr(
        scheduler,
        "_teardown_cron_agent",
        lambda agent, job_id: events.append(("teardown", job_id, agent)),
    )
    monkeypatch.setattr(scheduler, "_protected_fail_stop", threading.Event())
    monkeypatch.delenv("INVOCATION_ID", raising=False)

    with pytest.raises(scheduler.GatewayFailStopRequired):
        scheduler.run_one_job(
            {"id": JOB_ID, "execution_id": "execution-cli"},
            _fatal_restart_hook=scheduler._systemd_gateway_fail_stop,
        )

    assert [event[0] for event in events] == [
        "running",
        "run",
        "persist",
        "teardown",
    ]


def test_poisoned_tick_touches_no_lock_schedule_execution_or_handler(monkeypatch):
    events = []
    poison = threading.Event()
    poison.set()
    monkeypatch.setattr(scheduler, "_protected_fail_stop", poison)
    monkeypatch.setattr(
        scheduler,
        "_get_lock_paths",
        lambda: events.append("lock-path") or (Path("/unused"), Path("/unused/lock")),
    )
    monkeypatch.setattr(
        scheduler, "get_due_jobs", lambda: events.append("read-schedules") or []
    )
    monkeypatch.setattr(
        scheduler,
        "advance_next_runs",
        lambda _job_ids: events.append("advance-schedule"),
    )
    monkeypatch.setattr(
        scheduler,
        "create_execution",
        lambda *_args, **_kwargs: events.append("create-execution"),
    )
    monkeypatch.setattr(
        scheduler, "run_one_job", lambda *_args, **_kwargs: events.append("handler")
    )

    with pytest.raises(scheduler.GatewayFailStopRequired):
        scheduler.tick(verbose=False, sync=True)

    assert events == []


def test_fail_stop_hook_is_not_called_for_cooperative_or_ordinary_failures(
    monkeypatch,
):
    hook_calls = []

    for index, error in enumerate(
        (
            scheduler.ProtectedMutationFailure(
                reconciliation_keys=("issue-create-cooperative",),
                uncooperative=False,
            ),
            "TimeoutError: ordinary cron timeout",
        )
    ):
        events = []
        monkeypatch.setattr(scheduler, "_protected_fail_stop", threading.Event())
        _patch_run_one_job_pipeline(
            monkeypatch,
            run_result=(False, "failed output", "", error),
            events=events,
        )

        assert scheduler.run_one_job(
            {
                "id": f"job-{index}",
                "name": "bounded",
                "execution_id": f"execution-{index}",
            },
            _fatal_restart_hook=hook_calls.append,
        ) is True
        assert [event[0] for event in events].count("persist") == 1

    assert hook_calls == []


def test_gateway_fail_stop_exits_nonzero_only_under_systemd(monkeypatch):
    failure = scheduler.ProtectedMutationFailure(
        reconciliation_keys=("issue-create-systemd",),
        uncooperative=True,
    )
    exits = []

    class ExitCalled(BaseException):
        pass

    def fake_exit(code):
        exits.append(code)
        raise ExitCalled

    monkeypatch.setattr(scheduler.os, "_exit", fake_exit)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    with pytest.raises(scheduler.GatewayFailStopRequired):
        scheduler._systemd_gateway_fail_stop(failure)
    assert exits == []

    monkeypatch.setenv("INVOCATION_ID", "systemd-test")
    with pytest.raises(ExitCalled):
        scheduler._systemd_gateway_fail_stop(failure)
    assert exits == [70]
