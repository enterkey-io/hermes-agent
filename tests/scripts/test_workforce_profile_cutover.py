import hashlib
import json
from pathlib import Path

from scripts.workforce_backup import create_backup, verify_backup
from scripts.workforce_compile import compile_profiles
from scripts.workforce_profile_cutover import apply_cutover, preflight, rollback_cutover


ROOT = Path(__file__).parents[2]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_current_manifest_is_ready_after_emma_onboarding(tmp_path: Path):
    staging = tmp_path / "staging"
    compile_profiles(
        ROOT / "workforce/organization.yaml",
        ROOT / "workforce/templates/workforce-contract.md",
        staging,
    )
    report, _ = preflight(staging / "manifest.json")
    assert report["valid"] is True
    assert report["profiles"] == 22
    assert report["writes_ready"] == 22
    assert report["gates"] == []


def test_atomic_profile_cutover_creates_verified_archive_and_rolls_back(tmp_path: Path):
    profiles = tmp_path / "profiles"
    rows = []
    original = {}
    for name in ("aurora", "grace"):
        profile = profiles / name
        profile.mkdir(parents=True)
        source = profile / "AGENTS.md"
        source.write_text(f"original:{name}\n")
        original[name] = source.read_bytes()
        candidate = tmp_path / "candidates" / name / "AGENTS.md"
        candidate.parent.mkdir(parents=True)
        candidate.write_text(f"contract:{name}\noriginal:{name}\n")
        rows.append(
            {
                "agent": name,
                "status": "active",
                "source": str(source),
                "source_sha256": digest(source),
                "candidate": str(candidate),
                "candidate_sha256": digest(candidate),
            }
        )
    shared_target = tmp_path / "organization" / "organization.yaml"
    shared_candidate = tmp_path / "candidates" / "shared" / "organization.yaml"
    shared_candidate.parent.mkdir(parents=True)
    shared_candidate.write_text("schema_version: 1\n")
    rows.append({
        "agent": "shared:organization.yaml", "status": "active",
        "source": str(shared_target), "target": str(shared_target),
        "source_sha256": None, "candidate": str(shared_candidate),
        "candidate_sha256": digest(shared_candidate), "create_if_missing": True,
    })
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"profiles": rows}))
    full_backup = tmp_path / "full-backup"
    create_backup(profiles, full_backup)
    verify_backup(full_backup, tmp_path / "full-restore-test")
    immediate = tmp_path / "immediate"

    applied = apply_cutover(manifest, full_backup, immediate)
    assert applied["valid"] is True
    assert applied["applied"] is True
    assert applied["immediate_backup"]["files"] == 3
    assert json.loads((immediate / "restore-test.json").read_text())["valid"] is True
    assert (profiles / "aurora/AGENTS.md").read_text().startswith("contract:aurora")
    assert shared_target.read_text() == "schema_version: 1\n"

    restored = rollback_cutover(immediate)
    assert restored["valid"] is True
    for name in original:
        assert (profiles / name / "AGENTS.md").read_bytes() == original[name]
    assert not shared_target.exists()
