import json
from pathlib import Path

import pytest

from scripts import workforce_nanoclaw_deploy as deploy


def roots(tmp_path: Path) -> tuple[Path, Path]:
    candidate = tmp_path / "candidate"
    target = tmp_path / "target"
    for index, relative in enumerate(deploy.FILES):
        for root, prefix in ((candidate, "new"), (target, "old")):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{prefix}-{index}\n")
            path.chmod(0o755)
    return candidate, target


def test_plan_contains_only_paths_hashes_and_modes(tmp_path: Path):
    candidate, target = roots(tmp_path)
    plan = deploy.build_plan(candidate, target)
    assert plan["file_count"] == 4
    assert plan["mutation_performed"] is False
    assert all(item["change_required"] for item in plan["files"])


def test_apply_and_rollback_are_hash_guarded(tmp_path: Path):
    candidate, target = roots(tmp_path)
    plan = deploy.build_plan(candidate, target)
    rollback_dir = tmp_path / "rollback"
    result = deploy.apply_bundle(
        candidate_root=candidate,
        target_root=target,
        expected_plan=plan,
        rollback_dir=rollback_dir,
    )
    assert result["applied"] is True
    for relative in deploy.FILES:
        assert (target / relative).read_bytes() == (candidate / relative).read_bytes()

    restored = deploy.rollback_bundle(rollback_dir)
    assert restored["rolled_back"] is True
    for index, relative in enumerate(deploy.FILES):
        assert (target / relative).read_text() == f"old-{index}\n"


def test_apply_rejects_candidate_or_target_drift(tmp_path: Path):
    candidate, target = roots(tmp_path)
    plan = deploy.build_plan(candidate, target)
    (candidate / deploy.FILES[0]).write_text("different\n")
    with pytest.raises(ValueError, match="reviewed hash changed"):
        deploy.apply_bundle(
            candidate_root=candidate,
            target_root=target,
            expected_plan=plan,
            rollback_dir=tmp_path / "rollback",
        )


def test_rollback_refuses_post_cutover_drift(tmp_path: Path):
    candidate, target = roots(tmp_path)
    plan = deploy.build_plan(candidate, target)
    rollback_dir = tmp_path / "rollback"
    deploy.apply_bundle(
        candidate_root=candidate,
        target_root=target,
        expected_plan=plan,
        rollback_dir=rollback_dir,
    )
    (target / deploy.FILES[1]).write_text("post-cutover change\n")
    with pytest.raises(ValueError, match="live file changed"):
        deploy.rollback_bundle(rollback_dir)


def test_verified_backup_gate(tmp_path: Path):
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "restore-test.json").write_text(json.dumps({"valid": False}))
    with pytest.raises(ValueError, match="not valid"):
        deploy.verify_workforce_backup(backup)
