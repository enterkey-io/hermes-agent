from pathlib import Path
import os
import stat

import pytest

from scripts.workforce_backup import create_backup, verify_backup
from scripts import workforce_restore
from scripts.workforce_restore import restore


ROOT = Path(__file__).parents[2]


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and directory sync")
@pytest.mark.parametrize("fail_directory_sync", [False, True])
def test_atomic_write_preserves_inode_metadata_and_recovers_after_rename(
    tmp_path, monkeypatch, fail_directory_sync,
):
    target = tmp_path / "AGENTS.md"
    target.write_bytes(b"original")
    target.chmod(0o640)
    before = target.stat()
    real_replace = os.replace
    real_fsync = os.fsync
    replacements = []
    failed = False

    def checked_replace(source, destination):
        metadata = Path(source).stat()
        assert (metadata.st_uid, metadata.st_gid) == (before.st_uid, before.st_gid)
        assert stat.S_IMODE(metadata.st_mode) == 0o640
        replacements.append(Path(source).read_bytes())
        return real_replace(source, destination)

    def injected_fsync(fd):
        nonlocal failed
        if fail_directory_sync and not failed and stat.S_ISDIR(os.fstat(fd).st_mode):
            failed = True
            raise OSError("injected post-rename directory sync failure")
        return real_fsync(fd)

    monkeypatch.setattr(os, "replace", checked_replace)
    monkeypatch.setattr(os, "fsync", injected_fsync)
    if fail_directory_sync:
        with pytest.raises(OSError, match="post-rename"):
            workforce_restore._atomic_write(target, b"replacement", 0o640)
        assert replacements == [b"replacement", b"original"]
        assert target.read_bytes() == b"original"
    else:
        workforce_restore._atomic_write(target, b"replacement", 0o640)
        assert replacements == [b"replacement"]
        assert target.read_bytes() == b"replacement"
    after = target.stat()
    assert (after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)) == (
        before.st_uid, before.st_gid, 0o640,
    )
    assert list(tmp_path.glob(".AGENTS.md.*")) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory sync")
def test_atomic_new_file_is_removed_after_post_rename_sync_failure(tmp_path, monkeypatch):
    target = tmp_path / "AGENTS.md"
    real_fsync = os.fsync
    failed = False

    def injected_fsync(fd):
        nonlocal failed
        if not failed and stat.S_ISDIR(os.fstat(fd).st_mode):
            failed = True
            raise OSError("injected directory sync failure")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", injected_fsync)
    with pytest.raises(OSError, match="directory sync"):
        workforce_restore._atomic_write(target, b"new", 0o600)
    assert not target.exists()
    assert list(tmp_path.glob(".AGENTS.md.*")) == []


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
    assert report["profiles_without_pre_cutover_files"] == []
    assert len(report["restore_files"]) == len(profile_names) * 2
    for name in profile_names:
        assert (profiles / name / "AGENTS.md").read_text() == f"before:{name}\n"
        assert (profiles / name / "cron/jobs.json").read_text() == '{"jobs": []}\n'


def test_failed_restore_removes_targets_that_did_not_exist_before(tmp_path, monkeypatch):
    profiles = tmp_path / "profiles"
    org = __import__("hermes_cli.workforce_org", fromlist=["load_organization"]).load_organization(
        ROOT / "workforce/organization.yaml"
    )
    profile_names = {
        Path(item.profile_path).name
        for item in org.operational_agents(include_planned=False)
    }
    for name in profile_names:
        profile = profiles / name
        (profile / "cron").mkdir(parents=True)
        (profile / "AGENTS.md").write_text(f"baseline:{name}\n")
        (profile / "cron/jobs.json").write_text('{"jobs": []}\n')
    backup = tmp_path / "backup"
    create_backup(profiles, backup)
    verify_backup(backup, tmp_path / "scratch")

    for name in profile_names:
        (profiles / name / "AGENTS.md").unlink()
    original_write = workforce_restore._atomic_write
    calls = 0

    def fail_after_first(path, content, mode):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected restore failure")
        original_write(path, content, mode)

    monkeypatch.setattr(workforce_restore, "_atomic_write", fail_after_first)
    with pytest.raises(OSError, match="injected restore failure"):
        restore(
            backup=backup,
            organization=ROOT / "workforce/organization.yaml",
            profiles_root=profiles,
            scope="instructions",
            apply=True,
        )

    assert all(not (profiles / name / "AGENTS.md").exists() for name in profile_names)
