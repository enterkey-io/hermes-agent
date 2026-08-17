from __future__ import annotations

from pathlib import Path
import stat

import pytest

from hermes_cli import runbook_store
from hermes_cli.runbook_schema import RunbookValidationError
from tests.hermes_cli.test_runbook_validation import valid_markdown


def _metadata(slug: str = "morning-message") -> dict:
    return {
        "id": f"rb-{slug}",
        "slug": slug,
        "title": "Morning Message",
        "purpose": "Prepare and deliver the morning message.",
        "owner_profile": "grace",
        "status": "active",
        "runtime": {"kind": "script", "ref": "grace/scripts/morning.py"},
        "schedules": [],
        "steps": [{"step_key": "collect", "name": "Collect inputs"}],
        "inputs": {},
        "outputs": {},
        "permitted_writes": [],
        "approval_rules": {},
        "retry": {},
        "timeout": {},
        "deduplication": {},
        "related": {},
    }


def test_save_read_list_search_and_preview(tmp_path: Path) -> None:
    record = runbook_store.save_runbook(
        _metadata(),
        "# Morning Message\n\n| A | B |\n| - | - |\n| 1 | 2 |\n",
        root=tmp_path,
        approved_by="aurora",
    )

    assert record.slug == "morning-message"
    assert record.revision == f"sha256:{record.source_hash[:16]}"
    assert Path(record.path).exists()
    assert runbook_store.list_runbooks(root=tmp_path)[0].id == record.id
    assert runbook_store.search_runbooks("morning", root=tmp_path)[0].slug == record.slug

    html = runbook_store.render_preview(Path(record.path).read_text(encoding="utf-8"))
    assert "<table>" in html


def test_invalid_save_preserves_existing_file(tmp_path: Path) -> None:
    record = runbook_store.save_runbook(
        _metadata(),
        "# Valid\n",
        root=tmp_path,
        approved_by="aurora",
    )
    original = Path(record.path).read_text(encoding="utf-8")
    bad = _metadata()
    bad.pop("owner_profile")

    with pytest.raises(RunbookValidationError):
        runbook_store.save_runbook(bad, "# Invalid\n", root=tmp_path, approved_by="aurora")

    assert Path(record.path).read_text(encoding="utf-8") == original


def test_save_requires_approver(tmp_path: Path) -> None:
    with pytest.raises(PermissionError):
        runbook_store.save_runbook(_metadata(), "# Body\n", root=tmp_path)


def test_store_normalizes_trusted_directories_and_lock_owner_only(tmp_path: Path) -> None:
    root = tmp_path / "runbooks"
    root.mkdir(mode=0o775)
    root.chmod(0o775)

    record = runbook_store.save_runbook(
        _metadata(),
        "# Body\n",
        root=root,
        approved_by="aurora",
    )

    runbook_dir = Path(record.path).parent
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(runbook_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((runbook_dir / ".activation.lock").stat().st_mode) == 0o600


def test_revisions_diff_and_rollback(tmp_path: Path) -> None:
    first = runbook_store.save_runbook(
        _metadata(),
        "# Version 1\n",
        root=tmp_path,
        approved_by="aurora",
    )
    updated_meta = _metadata()
    updated_meta["title"] = "Morning Brief"
    runbook_store.save_runbook(
        updated_meta,
        "# Version 2\n",
        root=tmp_path,
        approved_by="aurora",
    )

    revisions = runbook_store.list_revisions(first.slug, root=tmp_path)
    assert len(revisions) == 1
    diff = runbook_store.diff_against_current(
        first.slug,
        valid_markdown(first.slug).replace("# Morning Message", "# Candidate"),
        root=tmp_path,
    )
    assert "Candidate" in diff

    rolled_back = runbook_store.rollback_revision(
        first.slug,
        revisions[0],
        root=tmp_path,
        approved_by="aurora",
    )
    assert rolled_back.title == "Morning Message"
    assert "# Version 1" in Path(rolled_back.path).read_text(encoding="utf-8")


def test_propose_edit_does_not_replace_active_runbook(tmp_path: Path) -> None:
    record = runbook_store.save_runbook(
        _metadata(),
        "# Active\n",
        root=tmp_path,
        approved_by="aurora",
    )
    active = Path(record.path).read_text(encoding="utf-8")
    proposed = valid_markdown().replace("# Morning Message", "# Proposed")

    proposal_path = runbook_store.propose_edit(
        record.slug,
        proposed,
        root=tmp_path,
        proposed_by="grace",
        summary="try a better title",
    )

    assert proposal_path.exists()
    assert "# Proposed" in proposal_path.read_text(encoding="utf-8")
    assert Path(record.path).read_text(encoding="utf-8") == active
