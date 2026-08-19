import json
import hashlib
from pathlib import Path

from scripts.workforce_compile import compile_profiles
from scripts.workforce_validate import validate


ROOT = Path(__file__).parents[2]


def test_whole_workforce_validator_accepts_compiled_fixture(tmp_path):
    staging = tmp_path / "staging"
    compile_profiles(
        ROOT / "workforce" / "organization.yaml",
        ROOT / "workforce" / "templates" / "workforce-contract.md",
        staging,
    )
    profiles = tmp_path / "profiles"
    records = []
    for friend in ("amy", "kourtnie"):
        path = profiles / friend / "AGENTS.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"{friend} unchanged")
        records.append({
            "path": f"profiles/{friend}/AGENTS.md",
            "type": "file",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "mtime_ns": path.stat().st_mtime_ns,
        })
    volatile = profiles / "kourtnie" / "skills" / ".usage.json"
    volatile.parent.mkdir(parents=True, exist_ok=True)
    volatile.write_text("runtime-before")
    records.append({
        "path": "profiles/kourtnie/skills/.usage.json",
        "type": "file",
        "sha256": hashlib.sha256(volatile.read_bytes()).hexdigest(),
        "mtime_ns": volatile.stat().st_mtime_ns,
    })
    volatile.write_text("runtime-after")
    cron_state = profiles / "kourtnie" / "cron" / "jobs.json"
    cron_state.parent.mkdir(parents=True, exist_ok=True)
    cron_state.write_text('{"next_run_at":"before"}')
    records.append({
        "path": "profiles/kourtnie/cron/jobs.json",
        "type": "file",
        "sha256": hashlib.sha256(cron_state.read_bytes()).hexdigest(),
        "mtime_ns": cron_state.stat().st_mtime_ns,
    })
    cron_state.write_text('{"next_run_at":"after"}')
    backup = tmp_path / "backup.json"
    backup.write_text(json.dumps({"files": records}))
    delivery = tmp_path / "delivery.json"
    delivery.write_text(json.dumps({
        "valid": True,
        "summary": {"unclassified": 0},
    }))
    report = validate(
        organization=ROOT / "workforce" / "organization.yaml",
        staging_root=staging,
        profiles_root=profiles,
        backup_manifest=backup,
        delivery_manifest=delivery,
    )
    assert report["valid"] is True
    assert report["operational_profiles"] == 22
    assert report["role_boundaries"]["chloe_directed_observer_only"] is True
    assert report["live_cutover_authorized"] is False
