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
