import json
from pathlib import Path

from scripts.workforce_delivery_inventory import build_manifest


ROOT = Path(__file__).parents[2]


def _write_jobs(root: Path, profile: str, jobs: list[dict]):
    path = root / profile / "cron" / "jobs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"jobs": jobs}))


def test_delivery_inventory_redacts_targets_and_detects_hidden_fallback(tmp_path):
    _write_jobs(
        tmp_path,
        "main",
        [
            {
                "id": "job-1",
                "name": "Update check",
                "enabled": True,
                "deliver": "telegram:12345",
                "prompt": "On failure call hermes send --platform telegram",
            }
        ],
    )
    report = build_manifest(
        tmp_path,
        ROOT / "workforce" / "organization.yaml",
        ROOT / "workforce" / "delivery-policy.yaml",
        ROOT / "workforce" / "buzz-topology.yaml",
    )
    job = report["jobs"][0]
    assert job["current_destination"] == "telegram:<redacted-target>"
    assert job["intended_room"] == "director-operations"
    assert job["validation_blocked_until_migrated"] is True
    assert {item["kind"] for item in job["hidden_delivery_paths"]} >= {
        "telegram", "direct_hermes_send"
    }
    assert job["direct_send_fallbacks"]
    assert job["registry_cron_status"] == "unregistered"
    assert job["registry_cron_mismatch"] is True
    assert job["legacy_paperclip_disposition"].startswith("historical-lookup-only")
    assert "12345" not in json.dumps(report)


def test_private_personal_exception_stays_out_of_shared_buzz(tmp_path):
    _write_jobs(
        tmp_path,
        "maya",
        [
            {
                "id": "ed046d36dac5",
                "name": "private check-in",
                "enabled": True,
                "deliver": "telegram:67890",
            }
        ],
    )
    report = build_manifest(
        tmp_path,
        ROOT / "workforce" / "organization.yaml",
        ROOT / "workforce" / "delivery-policy.yaml",
        ROOT / "workforce" / "buzz-topology.yaml",
    )
    job = report["jobs"][0]
    assert job["classification"] == "private-personal"
    assert job["intended_room"] is None
    assert job["migration_required"] is False


def test_lift_and_meeting_prep_route_to_executive_support(tmp_path):
    _write_jobs(
        tmp_path,
        "brenna",
        [{
            "id": "e0c7626e4467",
            "name": "LIFT pulse",
            "enabled": True,
            "deliver": "origin",
            "workflow_id": "wf-lift-pulse",
        }],
    )
    _write_jobs(
        tmp_path,
        "grace",
        [{
            "id": "be5404c1511b",
            "name": "LIFT meeting prep",
            "enabled": True,
            "deliver": "origin",
            "workflow_id": "wf-meeting-prep",
        }],
    )
    report = build_manifest(
        tmp_path,
        ROOT / "workforce" / "organization.yaml",
        ROOT / "workforce" / "delivery-policy.yaml",
        ROOT / "workforce" / "buzz-topology.yaml",
    )
    by_profile = {job["profile"]: job for job in report["jobs"]}
    assert by_profile["brenna"]["intended_room"] == "executive-support"
    assert by_profile["grace"]["intended_room"] == "executive-support"
    assert by_profile["brenna"]["quiet_success"] is False
    assert by_profile["grace"]["quiet_success"] is True


def test_existing_buzz_delivery_is_not_reported_as_an_unapplied_migration(tmp_path):
    _write_jobs(
        tmp_path,
        "main",
        [{
            "id": "job-1",
            "name": "Operations report",
            "enabled": True,
            "deliver": "buzz:11111111-1111-1111-1111-111111111111",
            "workflow_id": "wf-1",
        }],
    )
    report = build_manifest(
        tmp_path,
        ROOT / "workforce" / "organization.yaml",
        ROOT / "workforce" / "delivery-policy.yaml",
        ROOT / "workforce" / "buzz-topology.yaml",
    )
    job = report["jobs"][0]
    assert job["migration_required"] is False
    assert job["staged_change_not_executed"] is None
    assert job["destination_verification"]
    assert report["summary"]["migration_required"] == 0


def test_weekday_one_thing_routes_to_exec_support_but_personal_checkin_stays_private(tmp_path):
    _write_jobs(tmp_path, "grace", [
        {"id": "019c9f963374", "name": "LIFT One Thing", "enabled": True, "deliver": "buzz:old", "workflow_id": "wf-morning"},
        {"id": "537d8032bdaf", "name": "personal heartbeat", "enabled": True, "deliver": "telegram:123", "workflow_id": "wf-heartbeat"},
    ])
    report = build_manifest(
        tmp_path, ROOT / "workforce/organization.yaml",
        ROOT / "workforce/delivery-policy.yaml", ROOT / "workforce/buzz-topology.yaml",
    )
    by_id = {job["job_id"]: job for job in report["jobs"]}
    assert by_id["019c9f963374"]["classification"] == "team"
    assert by_id["019c9f963374"]["intended_room"] == "executive-support"
    assert by_id["537d8032bdaf"]["classification"] == "private-personal"
    assert by_id["537d8032bdaf"]["intended_room"] is None


def test_xenia_critical_trading_alerts_preserve_photon_imessage(tmp_path):
    _write_jobs(tmp_path, "xenia", [
        {"id": "a1fe3851b74d", "name": "VT report", "enabled": True, "deliver": "photon:+15551234567", "workflow_id": "wf-vt"},
        {"id": "5e918872bd5a", "name": "VT late reconciliation", "enabled": True, "deliver": "photon:+15551234567", "workflow_id": "wf-vt"},
    ])
    report = build_manifest(
        tmp_path, ROOT / "workforce/organization.yaml",
        ROOT / "workforce/delivery-policy.yaml", ROOT / "workforce/buzz-topology.yaml",
    )
    assert all(job["classification"] == "private-personal" for job in report["jobs"])
    assert all(job["current_destination"] == "photon:<redacted-target>" for job in report["jobs"])
    assert all(job["migration_required"] is False for job in report["jobs"])


def test_paperclip_route_is_explicitly_dispositioned(tmp_path):
    _write_jobs(
        tmp_path,
        "main",
        [{
            "id": "job-1",
            "name": "legacy task creation",
            "enabled": True,
            "deliver": "origin",
            "prompt": "Create a task in Paperclip after the check",
            "workflow_id": "wf-1",
        }],
    )
    report = build_manifest(
        tmp_path,
        ROOT / "workforce" / "organization.yaml",
        ROOT / "workforce" / "delivery-policy.yaml",
        ROOT / "workforce" / "buzz-topology.yaml",
    )
    job = report["jobs"][0]
    assert job["legacy_paperclip_disposition"] == "remove-active-paperclip-route"
    assert job["registry_cron_mismatch"] is False
    assert report["summary"]["active_paperclip_routes"] == 1


def test_archive_only_paperclip_prohibition_is_not_an_active_route(tmp_path):
    _write_jobs(
        tmp_path,
        "main",
        [{
            "id": "job-1",
            "name": "archive-safe check",
            "enabled": True,
            "deliver": "origin",
            "prompt": "Do not create Paperclip issues; Paperclip is archive-only.",
            "workflow_id": "wf-1",
        }],
    )
    report = build_manifest(
        tmp_path,
        ROOT / "workforce" / "organization.yaml",
        ROOT / "workforce" / "delivery-policy.yaml",
        ROOT / "workforce" / "buzz-topology.yaml",
    )
    job = report["jobs"][0]
    assert job["legacy_paperclip_disposition"].startswith("archive-only")
    assert report["summary"]["active_paperclip_routes"] == 0


def test_backup_only_paperclip_language_is_not_an_active_route(tmp_path):
    _write_jobs(
        tmp_path,
        "alina",
        [{
            "id": "job-1",
            "name": "archive inventory",
            "enabled": True,
            "deliver": "buzz:11111111-1111-1111-1111-111111111111",
            "prompt": (
                "Paperclip is read-only inventory/archive only. "
                "Paperclip is backup-only archive."
            ),
            "workflow_id": "wf-1",
        }],
    )
    report = build_manifest(
        tmp_path,
        ROOT / "workforce" / "organization.yaml",
        ROOT / "workforce" / "delivery-policy.yaml",
        ROOT / "workforce" / "buzz-topology.yaml",
    )
    assert report["summary"]["active_paperclip_routes"] == 0
    assert report["jobs"][0]["legacy_paperclip_disposition"].startswith("archive-only")
