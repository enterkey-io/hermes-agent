"""Trusted scheduler issuance and teardown of cron execution capabilities."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from agent.execution_capabilities import (
    _bind_execution_context,
    cron_job_capability,
    execution_context_allows,
)
from cron.scheduler import run_job


JOB_ID = "8ff7fa2ddb8b"


@pytest.mark.parametrize("run_fails", [False, True])
def test_run_job_passes_internal_context_and_always_revokes_it(
    tmp_path,
    run_fails,
):
    job = {"id": JOB_ID, "name": "bounded", "prompt": "hello"}
    fake_db = MagicMock()
    seen = {}

    class FakeAgent:
        def __init__(self, *args, **kwargs):
            context = kwargs.get("execution_context")
            seen["context"] = context
            seen["constructor_env"] = os.environ.get("HERMES_CRON_JOB_ID")
            _bind_execution_context(context, self)
            seen["live_in_constructor"] = execution_context_allows(
                context,
                cron_job_capability(JOB_ID),
            )

        def run_conversation(self, prompt):
            seen["run_env"] = os.environ.get("HERMES_CRON_JOB_ID")
            seen["live_in_run"] = execution_context_allows(
                seen["context"],
                cron_job_capability(JOB_ID),
            )
            if run_fails:
                raise RuntimeError("test run failure")
            return {"final_response": "ok"}

    with (
        patch("cron.scheduler._hermes_home", tmp_path),
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
        success, _output, final_response, error = run_job(job)

    assert success is not run_fails
    assert final_response == ("" if run_fails else "ok")
    assert (error is not None) is run_fails
    assert seen["live_in_constructor"] is True
    assert seen["live_in_run"] is True
    assert seen["constructor_env"] is None
    assert seen["run_env"] is None
    assert execution_context_allows(
        seen["context"],
        cron_job_capability(JOB_ID),
    ) is False
