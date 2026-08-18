from pathlib import Path

from scripts.workforce_backup import create_backup, verify_backup
from scripts.workforce_restore import restore


ROOT = Path(__file__).parents[2]


def test_instruction_and_delivery_rollback_restores_complete_active_set(tmp_path):
    profiles = tmp_path / "profiles"
    profile_names = {
        Path(item.profile_path).name
        for item in __import__("hermes_cli.workforce_org", fromlist=["load_organization"])
        .load_organization(ROOT / "workforce/organization.yaml")
        .operational_agents(include_planned=False)
    }
    for name in profile_names:
        profile = profiles / name
        (profile / "cron").mkdir(parents=True)
        (profile / "AGENTS.md").write_text(f"before:{name}\n")
        (profile / "cron/jobs.json").write_text('{"jobs": []}\n')
    backup = tmp_path / "backup"
    create_backup(profiles, backup)
    verify_backup(backup, tmp_path / "scratch")
    for name in profile_names:
        (profiles / name / "AGENTS.md").write_text("after\n")
        (profiles / name / "cron/jobs.json").write_text('{"jobs": ["after"]}\n')

    report = restore(
        backup=backup,
        organization=ROOT / "workforce/organization.yaml",
        profiles_root=profiles,
        scope="all",
        apply=True,
    )

    assert report["applied"] is True
    assert report["profiles_without_pre_cutover_files"] == ["chloe"]
    assert len(report["restore_files"]) == len(profile_names) * 2
    for name in profile_names:
        assert (profiles / name / "AGENTS.md").read_text() == f"before:{name}\n"
        assert (profiles / name / "cron/jobs.json").read_text() == '{"jobs": []}\n'
