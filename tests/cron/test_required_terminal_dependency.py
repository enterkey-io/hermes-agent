"""Real terminal-path coverage for required Cron dependencies."""

from __future__ import annotations

import json

import pytest

import cron.scheduler as scheduler
from cron.executions import latest_execution
from hermes_cli import workflow_registry as registry
from tools.terminal_tool import _handle_terminal


WORKFLOW_ID = "wf-required-terminal-test"


@pytest.fixture
def workflow(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: tmp_path)
    with registry.connect_closing() as conn:
        registry.create_definition(
            conn,
            id=WORKFLOW_ID,
            slug="required-terminal-test",
            name="Required terminal test",
            owner_profile="default",
            status="active",
            runtime_kind="hermes",
        )
        registry.replace_steps(
            conn,
            WORKFLOW_ID,
            [{"step_key": "run", "position": 0, "name": "Run"}],
        )
    return tmp_path


def _run(monkeypatch, tmp_path, run_job, *, required=True):
    marked = []
    delivered = []
    monkeypatch.setattr(scheduler, "run_job", run_job)
    monkeypatch.setattr(
        scheduler, "save_job_output", lambda *_args: tmp_path / "output.md"
    )
    monkeypatch.setattr(
        scheduler,
        "_deliver_result",
        lambda _job, content, **_kwargs: delivered.append(content),
    )
    monkeypatch.setattr(
        scheduler,
        "mark_job_run",
        lambda job_id, success, error=None, **kwargs: marked.append((
            job_id,
            success,
            error,
            kwargs,
        )),
    )
    job = {
        "id": "required-terminal-job",
        "name": "Required terminal job",
        "deliver": "local",
        "workflow_id": WORKFLOW_ID,
        "workflow_step_key": "run",
        "track_workflow_status": True,
    }
    if required:
        job.update(
            required_tool_dependencies=["terminal"],
            required_tool_dependency_mode="when_invoked",
        )

    assert scheduler.run_one_job(job) is True
    return marked, delivered


def test_real_terminal_failure_overrides_model_completed_marker(monkeypatch, workflow):
    def run_job(_job, **_kwargs):
        first = json.loads(
            _handle_terminal({
                "command": "/bin/sh -c 'exit 7'",
                "workdir": str(workflow),
            })
        )
        assert first["exit_code"] == 7
        second = json.loads(
            _handle_terminal({
                "command": "/bin/sh -c 'exit 0'",
                "workdir": str(workflow),
            })
        )
        assert second["exit_code"] == 0
        return True, "raw output", "[SILENT]\n[WORKFLOW_STATUS:completed]", None

    marked, delivered = _run(monkeypatch, workflow, run_job)

    assert delivered == [
        "⚠️ Cron 'Required terminal job' failed: Required tool dependency "
        "degraded: unsuccessful: terminal (nonzero_exit)"
    ]
    assert marked == [
        (
            "required-terminal-job",
            False,
            "Required tool dependency degraded: unsuccessful: terminal (nonzero_exit)",
            {
                "delivery_error": None,
                "workflow_status": "failed",
                "dependency_status": "degraded",
                "dependency_outcome": {
                    "required": ["terminal"],
                    "successful": [],
                    "failed": [{"tool": "terminal", "reasons": ["nonzero_exit"]}],
                    "missing": [],
                },
            },
        )
    ]
    execution = latest_execution("required-terminal-job")
    assert execution is not None
    assert execution["status"] == "failed"
    assert execution["error"] == marked[0][2]
    with registry.connect_closing() as conn:
        run = registry.list_runs(conn, WORKFLOW_ID)[0]
        step = conn.execute(
            "SELECT * FROM workflow_step_runs WHERE workflow_run_id=?",
            (run.id,),
        ).fetchone()
    assert run.status == "failed"
    assert run.error == marked[0][2]
    assert step["status"] == "failed"
    assert step["error"] == marked[0][2]


def test_real_terminal_success_preserves_completed_workflow(monkeypatch, workflow):
    def run_job(_job, **_kwargs):
        result = json.loads(
            _handle_terminal({
                "command": "/bin/sh -c 'exit 0'",
                "workdir": str(workflow),
            })
        )
        assert result["exit_code"] == 0
        return True, "raw output", "[SILENT]\n[WORKFLOW_STATUS:completed]", None

    marked, delivered = _run(monkeypatch, workflow, run_job)

    assert delivered == []
    assert marked[0][1] is True
    assert marked[0][2] is None
    assert marked[0][3]["workflow_status"] == "completed"
    assert marked[0][3]["dependency_status"] == "healthy"
    assert marked[0][3]["dependency_outcome"]["successful"] == ["terminal"]
    assert latest_execution("required-terminal-job")["status"] == "completed"


def test_ordinary_job_retains_recoverable_terminal_behavior(monkeypatch, workflow):
    def run_job(_job, **_kwargs):
        result = json.loads(
            _handle_terminal({
                "command": "/bin/sh -c 'exit 7'",
                "workdir": str(workflow),
            })
        )
        assert result["exit_code"] == 7
        return True, "raw output", "[SILENT]\n[WORKFLOW_STATUS:completed]", None

    marked, delivered = _run(monkeypatch, workflow, run_job, required=False)

    assert delivered == []
    assert marked[0][1] is True
    assert marked[0][3]["workflow_status"] == "completed"
    assert "dependency_outcome" not in marked[0][3]


def test_terminal_failure_is_sticky_across_identical_retry(monkeypatch, workflow):
    from tools.required_dependency_runtime import activate, reset

    token, state = activate(["terminal"])
    try:
        command = {"command": "/bin/sh -c 'exit 9'", "workdir": str(workflow)}
        assert json.loads(_handle_terminal(command))["exit_code"] == 9
        command["command"] = "/bin/sh -c 'exit 0'"
        assert json.loads(_handle_terminal(command))["exit_code"] == 0

        summary = state.finalize()
    finally:
        reset(token)

    assert summary["failed"] == [{"tool": "terminal", "reasons": ["nonzero_exit"]}]


def test_expected_nonzero_terminal_meaning_is_not_a_failure(monkeypatch, workflow):
    from tools.required_dependency_runtime import activate, reset

    token, state = activate(["terminal"])
    try:
        result = json.loads(
            _handle_terminal({
                "command": "grep absent /dev/null",
                "workdir": str(workflow),
            })
        )
        assert result["exit_code"] == 1
        assert result["exit_code_meaning"]
        summary = state.finalize()
    finally:
        reset(token)

    assert summary["successful"] == ["terminal"]
    assert summary["failed"] == []


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        ("/bin/sh -c 'exit 130'", "nonzero_exit"),
        ("/bin/sh -c 'kill -TERM $$'", "nonzero_exit"),
    ],
)
def test_signal_notes_do_not_turn_terminal_failures_into_success(
    monkeypatch,
    workflow,
    command,
    reason,
):
    from tools.required_dependency_runtime import activate, reset

    token, state = activate(["terminal"])
    try:
        result = json.loads(
            _handle_terminal({"command": command, "workdir": str(workflow)})
        )
        assert result["exit_code"] != 0
        summary = state.finalize()
    finally:
        reset(token)

    assert summary["successful"] == []
    assert summary["failed"] == [{"tool": "terminal", "reasons": [reason]}]


def test_required_background_terminal_stays_unverified(monkeypatch):
    from tools.required_dependency_runtime import activate, reset
    import tools.terminal_tool as terminal_module

    monkeypatch.setattr(
        terminal_module,
        "terminal_tool",
        lambda **_kwargs: json.dumps({
            "output": "Background process started",
            "exit_code": 0,
        }),
    )
    token, state = activate(["terminal"])
    try:
        _handle_terminal({"command": "long-task", "background": True})
        summary = state.finalize()
    finally:
        reset(token)

    assert summary["successful"] == []
    assert summary["failed"] == [{"tool": "terminal", "reasons": ["pending"]}]


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (
            json.dumps({
                "status": "degraded",
                "exit_code": -1,
                "error": "backend down",
            }),
            "backend_degraded",
        ),
        (
            json.dumps({
                "status": "error",
                "exit_code": -1,
                "error": "execution error",
            }),
            "tool_error",
        ),
        ("not-json", "malformed_result"),
        (
            json.dumps({
                "output": "[Command interrupted]",
                "exit_code": 130,
                "error": None,
            }),
            "interrupted",
        ),
    ],
)
def test_terminal_failure_classes_are_bounded_and_redacted(
    monkeypatch,
    payload,
    reason,
):
    from tools.required_dependency_runtime import activate, reset
    import tools.terminal_tool as terminal_module

    monkeypatch.setattr(
        terminal_module,
        "terminal_tool",
        lambda **_kwargs: payload,
    )
    token, state = activate(["terminal"])
    try:
        _handle_terminal({"command": "echo must-not-persist"})
        summary = state.finalize()
    finally:
        reset(token)

    assert summary["failed"] == [{"tool": "terminal", "reasons": [reason]}]
    assert "must-not-persist" not in json.dumps(summary)
