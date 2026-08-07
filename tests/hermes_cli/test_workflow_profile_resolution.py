from __future__ import annotations

from pathlib import Path

from hermes_cli import workflow_registry as reg
from hermes_constants import set_hermes_home_override, reset_hermes_home_override


def test_registry_default_path_uses_shared_root_for_profile_home(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / ".hermes"
    profile_home = root / "profiles" / "grace"
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))

    assert reg.workflow_registry_db_path() == root / "workflow_registry.db"


def test_explicit_db_path_ignores_profile_context_override(tmp_path: Path) -> None:
    token = set_hermes_home_override(tmp_path / "profiles" / "grace")
    try:
        db_path = tmp_path / "shared" / "workflow_registry.db"
        with reg.connect_closing(db_path) as conn:
            created = reg.create_definition(
                conn,
                slug="explicit",
                name="Explicit",
                owner_profile="grace",
                status="active",
                runtime_kind="hermes",
            )
            assert reg.get_definition(conn, created.id).slug == "explicit"
        assert db_path.exists()
    finally:
        reset_hermes_home_override(token)
