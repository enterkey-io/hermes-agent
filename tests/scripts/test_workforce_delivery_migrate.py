import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import workforce_delivery_migrate
from scripts.workforce_delivery_migrate import migrate


ROOT = Path(__file__).parents[2]


def test_delivery_migration_script_runs_directly_outside_the_repo(tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/workforce_delivery_migrate.py"), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--room-map" in result.stdout


def test_delivery_migration_rewrites_team_route_without_touching_friend(tmp_path):
    profiles = tmp_path / "profiles"
    main_jobs = profiles / "main" / "cron" / "jobs.json"
    main_jobs.parent.mkdir(parents=True)
    main_jobs.write_text(json.dumps({"jobs": [{
        "id": "root-job", "name": "check", "enabled": True,
        "deliver": "telegram:123", "prompt": "Send the exception to Telegram.",
        "workflow_id": "wf-root-job"
    }]}))
    friend_jobs = profiles / "amy" / "cron" / "jobs.json"
    friend_jobs.parent.mkdir(parents=True)
    friend_jobs.write_text(json.dumps({"jobs": [{
        "id": "friend-job", "enabled": True, "deliver": "telegram:456"
    }]}))
    before_friend = friend_jobs.read_bytes()
    room_map = {
        "director-operations": "11111111-1111-1111-1111-111111111111"
    }
    report = migrate(
        profiles_root=profiles,
        organization=ROOT / "workforce" / "organization.yaml",
        policy=ROOT / "workforce" / "delivery-policy.yaml",
        topology=ROOT / "workforce" / "buzz-topology.yaml",
        room_map=room_map,
        apply=True,
    )
    assert report["applied"] is True
    updated = json.loads(main_jobs.read_text())["jobs"][0]
    assert updated["deliver"] == "buzz:11111111-1111-1111-1111-111111111111"
    assert "Telegram" not in updated["prompt"]
    assert "[WORKFORCE DELIVERY POLICY]" in updated["prompt"]
    assert "Routine success is silent" in updated["prompt"]
    assert friend_jobs.read_bytes() == before_friend
    assert report["changed_jobs"] == 1
    assert report["changes"][0]["prompt_changed"] is True
    assert report["changes"][0]["destination_changed"] is True

    second = migrate(
        profiles_root=profiles,
        organization=ROOT / "workforce" / "organization.yaml",
        policy=ROOT / "workforce" / "delivery-policy.yaml",
        topology=ROOT / "workforce" / "buzz-topology.yaml",
        room_map=room_map,
        apply=False,
    )
    assert second["changed_jobs"] == 0
    assert second["changes"] == []


def test_delivery_apply_blocks_unregistered_cron_job(tmp_path):
    profiles = tmp_path / "profiles"
    jobs = profiles / "main" / "cron" / "jobs.json"
    jobs.parent.mkdir(parents=True)
    jobs.write_text(json.dumps({"jobs": [{
        "id": "root-job", "name": "check", "enabled": True,
        "deliver": "origin", "prompt": "Check status."
    }]}))
    with pytest.raises(ValueError, match="blocked until every enabled Cron job"):
        migrate(
            profiles_root=profiles,
            organization=ROOT / "workforce" / "organization.yaml",
            policy=ROOT / "workforce" / "delivery-policy.yaml",
            topology=ROOT / "workforce" / "buzz-topology.yaml",
            room_map={"director-operations": "11111111-1111-1111-1111-111111111111"},
            apply=True,
        )


def test_delivery_migration_replaces_existing_managed_room_policy(tmp_path):
    profiles = tmp_path / "profiles"
    jobs = profiles / "grace" / "cron" / "jobs.json"
    jobs.parent.mkdir(parents=True)
    jobs.write_text(json.dumps({"jobs": [{
        "id": "be5404c1511b",
        "name": "LIFT meeting prep",
        "enabled": True,
        "deliver": "buzz:99999999-9999-9999-9999-999999999999",
        "prompt": (
            "Prepare the brief.\n\n[WORKFORCE DELIVERY POLICY]\n"
            "Return team-facing output only through the Cron destination for Buzz room "
            "`lift-accountability`. Do not call a platform messaging tool.\n"
        ),
        "workflow_id": "wf-meeting-prep",
    }]}))
    report = migrate(
        profiles_root=profiles,
        organization=ROOT / "workforce" / "organization.yaml",
        policy=ROOT / "workforce" / "delivery-policy.yaml",
        topology=ROOT / "workforce" / "buzz-topology.yaml",
        room_map={"executive-support": "11111111-1111-1111-1111-111111111111"},
        apply=True,
    )
    assert report["applied"] is True
    updated = json.loads(jobs.read_text())["jobs"][0]
    assert updated["deliver"] == "buzz:11111111-1111-1111-1111-111111111111"
    assert "`executive-support`" in updated["prompt"]
    assert "`lift-accountability`" not in updated["prompt"]
    assert updated["prompt"].count("[WORKFORCE DELIVERY POLICY]") == 1


def test_canonical_runbook_delivery_contract_is_already_compliant(tmp_path):
    profiles = tmp_path / "profiles"
    jobs = profiles / "main" / "cron" / "jobs.json"
    jobs.parent.mkdir(parents=True)
    prompt = (
        "Run the canonical workflow. Use only the Cron-configured Buzz destination; "
        "do not call a platform messaging tool or use a fallback."
    )
    jobs.write_text(json.dumps({"jobs": [{
        "id": "main-job",
        "name": "operations outcome review",
        "enabled": True,
        "deliver": "buzz:11111111-1111-1111-1111-111111111111",
        "prompt": prompt,
        "workflow_id": "wf-main-job",
    }]}))
    report = migrate(
        profiles_root=profiles,
        organization=ROOT / "workforce" / "organization.yaml",
        policy=ROOT / "workforce" / "delivery-policy.yaml",
        topology=ROOT / "workforce" / "buzz-topology.yaml",
        room_map={
            "director-operations": "11111111-1111-1111-1111-111111111111"
        },
        apply=False,
    )
    assert report["changed_jobs"] == 0
    assert json.loads(jobs.read_text())["jobs"][0]["prompt"] == prompt


def test_delivery_apply_validates_every_profile_before_first_write(tmp_path):
    profiles = tmp_path / "profiles"
    grace_jobs = profiles / "grace" / "cron" / "jobs.json"
    grace_jobs.parent.mkdir(parents=True)
    grace_jobs.write_text(json.dumps({"jobs": [{
        "id": "grace-job", "name": "meeting prep", "enabled": True,
        "deliver": "telegram:1", "prompt": "Send to Telegram.",
        "workflow_id": "wf-grace-job",
    }]}))
    main_jobs = profiles / "main" / "cron" / "jobs.json"
    main_jobs.parent.mkdir(parents=True)
    main_jobs.write_text(json.dumps({"jobs": [{
        "id": "main-job", "name": "operations check", "enabled": True,
        "deliver": "telegram:2", "prompt": "Send to Telegram.",
        "workflow_id": "wf-main-job",
    }]}))
    grace_before = grace_jobs.read_bytes()
    main_before = main_jobs.read_bytes()

    with pytest.raises(ValueError, match="missing room UUID for director-operations"):
        migrate(
            profiles_root=profiles,
            organization=ROOT / "workforce" / "organization.yaml",
            policy=ROOT / "workforce" / "delivery-policy.yaml",
            topology=ROOT / "workforce" / "buzz-topology.yaml",
            room_map={
                "executive-support": "11111111-1111-1111-1111-111111111111"
            },
            apply=True,
        )

    assert grace_jobs.read_bytes() == grace_before
    assert main_jobs.read_bytes() == main_before


def test_delivery_apply_rolls_back_an_earlier_write_on_io_failure(tmp_path, monkeypatch):
    profiles = tmp_path / "profiles"
    grace_jobs = profiles / "grace" / "cron" / "jobs.json"
    grace_jobs.parent.mkdir(parents=True)
    grace_jobs.write_text(json.dumps({"jobs": [{
        "id": "grace-job", "name": "meeting prep", "enabled": True,
        "deliver": "telegram:1", "prompt": "Send to Telegram.",
        "workflow_id": "wf-grace-job",
    }]}))
    main_jobs = profiles / "main" / "cron" / "jobs.json"
    main_jobs.parent.mkdir(parents=True)
    main_jobs.write_text(json.dumps({"jobs": [{
        "id": "main-job", "name": "operations check", "enabled": True,
        "deliver": "telegram:2", "prompt": "Send to Telegram.",
        "workflow_id": "wf-main-job",
    }]}))
    grace_before = grace_jobs.read_bytes()
    main_before = main_jobs.read_bytes()
    original_write = workforce_delivery_migrate._atomic_write_json
    calls = 0

    def fail_second(path, payload):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected delivery write failure")
        original_write(path, payload)

    monkeypatch.setattr(workforce_delivery_migrate, "_atomic_write_json", fail_second)
    with pytest.raises(OSError, match="injected delivery write failure"):
        migrate(
            profiles_root=profiles,
            organization=ROOT / "workforce" / "organization.yaml",
            policy=ROOT / "workforce" / "delivery-policy.yaml",
            topology=ROOT / "workforce" / "buzz-topology.yaml",
            room_map={
                "executive-support": "11111111-1111-1111-1111-111111111111",
                "director-operations": "22222222-2222-2222-2222-222222222222",
            },
            apply=True,
        )

    assert grace_jobs.read_bytes() == grace_before
    assert main_jobs.read_bytes() == main_before
