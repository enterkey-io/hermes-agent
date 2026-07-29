"""Cron creation/update inference-contract persistence."""

from __future__ import annotations

from datetime import datetime, timedelta
import json

import pytest


CONTRACT_FIELDS = (
    "provider",
    "model",
    "reasoning_effort",
    "speed",
)


@pytest.fixture
def inference_profile(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text(
        "model:\n"
        "  default: profile-model\n"
        "  provider: profile-provider\n"
        "agent:\n"
        "  reasoning_effort: medium\n"
        "  service_tier: fast\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_MODEL", raising=False)

    def resolve_runtime_provider(**kwargs):
        return {
            "provider": kwargs.get("requested") or "profile-provider",
            "requested_provider": kwargs.get("requested"),
        }

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        resolve_runtime_provider,
    )
    return home


def assert_complete_contract(job, expected):
    for field in CONTRACT_FIELDS:
        assert job[field] == expected[field]
        assert job[f"{field}_snapshot"] == expected[field]
    assert "service_tier" not in job


def test_create_job_resolves_and_persists_fully_defaulted_profile(inference_profile):
    from cron.jobs import create_job, get_job

    job = create_job(prompt="default contract", schedule="every 1h")

    expected = {
        "provider": "profile-provider",
        "model": "profile-model",
        "reasoning_effort": "medium",
        "speed": "fast",
    }
    assert_complete_contract(job, expected)
    assert_complete_contract(get_job(job["id"]), expected)


def test_registered_agent_tool_uses_user_owned_profile_contract(inference_profile):
    from cron.jobs import get_job
    from tools.cronjob_tools import registry

    result = json.loads(
        registry.get_entry("cronjob").handler(
            {
                "action": "create",
                "prompt": "profile contract",
                "schedule": "every 2h",
                "model": {
                    "provider": "explicit-provider",
                    "model": "explicit-model",
                    "reasoning_effort": "high",
                    "speed": "standard",
                },
            }
        )
    )

    assert result["success"] is True
    persisted = get_job(result["job_id"])
    assert_complete_contract(
        persisted,
        {
            "provider": "profile-provider",
            "model": "profile-model",
            "reasoning_effort": "medium",
            "speed": "fast",
        },
    )


def test_public_create_path_persists_recurring_contract(inference_profile):
    from cron.jobs import get_job
    from tools.cronjob_tools import cronjob

    result = json.loads(
        cronjob(
            action="create",
            prompt="recurring contract",
            schedule="every 15m",
        )
    )

    assert result["success"] is True
    persisted = get_job(result["job_id"])
    assert persisted["schedule"]["kind"] == "interval"
    assert_complete_contract(
        persisted,
        {
            "provider": "profile-provider",
            "model": "profile-model",
            "reasoning_effort": "medium",
            "speed": "fast",
        },
    )


def test_public_create_path_persists_one_shot_contract(inference_profile):
    from cron.jobs import get_job
    from tools.cronjob_tools import cronjob

    run_at = (datetime.now().astimezone() + timedelta(minutes=10)).isoformat()
    result = json.loads(
        cronjob(
            action="create",
            prompt="one shot contract",
            schedule=run_at,
        )
    )

    assert result["success"] is True
    persisted = get_job(result["job_id"])
    assert persisted["schedule"]["kind"] == "once"
    assert persisted["repeat"]["times"] == 1
    assert_complete_contract(
        persisted,
        {
            "provider": "profile-provider",
            "model": "profile-model",
            "reasoning_effort": "medium",
            "speed": "fast",
        },
    )


def test_no_agent_job_is_schema_permitted_contract_exception(inference_profile):
    from cron.jobs import create_job

    job = create_job(
        prompt=None,
        schedule="every 5m",
        script="receipt.sh",
        no_agent=True,
    )

    for field in CONTRACT_FIELDS:
        assert job.get(field) is None
        assert job.get(f"{field}_snapshot") is None


@pytest.mark.parametrize(
    ("service_tier", "expected_speed"),
    (("priority", "fast"), ("normal", "standard")),
)
def test_create_job_normalizes_legacy_service_tier_at_input_boundary(
    inference_profile,
    service_tier,
    expected_speed,
):
    from cron.jobs import create_job

    job = create_job(
        prompt="legacy tier",
        schedule="every 1h",
        service_tier=service_tier,
    )

    assert job["speed"] == expected_speed
    assert job["speed_snapshot"] == expected_speed
    assert "service_tier" not in job


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("provider", "updated-provider"),
        ("model", "updated-model"),
        ("reasoning_effort", "xhigh"),
        ("speed", "standard"),
    ),
)
def test_update_job_changes_axis_and_matching_snapshot_atomically(
    inference_profile,
    field,
    value,
):
    from cron.jobs import create_job, get_job, update_job

    job = create_job(prompt="atomic update", schedule="every 1h")
    updated = update_job(job["id"], {field: value})

    assert updated[field] == value
    assert updated[f"{field}_snapshot"] == value
    assert get_job(job["id"])[f"{field}_snapshot"] == value
    for other in CONTRACT_FIELDS:
        assert updated[f"{other}_snapshot"] == updated[other]


def test_incomplete_enabled_agent_job_is_rejected_before_persistence(
    tmp_path,
    monkeypatch,
):
    from cron.jobs import create_job, load_jobs

    home = tmp_path / "incomplete-profile"
    home.mkdir()
    (home / "config.yaml").write_text(
        "agent:\n  reasoning_effort: medium\n  service_tier: fast\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_MODEL", raising=False)
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **_: {"provider": "profile-provider"},
    )

    with pytest.raises(ValueError, match="complete inference contract"):
        create_job(prompt="must not persist", schedule="every 1h")

    assert load_jobs() == []
