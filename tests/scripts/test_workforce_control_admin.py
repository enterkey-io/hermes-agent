from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from hermes_cli import kanban_db
from scripts import workforce_control_admin


def _database(path: Path) -> Path:
    with kanban_db.connect_closing(path):
        pass
    return path


def test_status_does_not_initialize_schema(tmp_path, capsys):
    database = _database(tmp_path / "kanban.db")
    assert workforce_control_admin.main(["--database", str(database), "status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["available"] is False
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='wc_runtime'").fetchone()[0] == 0


def test_init_and_shadow_require_confirmation_and_make_verified_backups(tmp_path, capsys):
    database = _database(tmp_path / "kanban.db")
    first_backup = tmp_path / "backups" / "before-init.db"
    with pytest.raises(PermissionError):
        workforce_control_admin.main([
            "--database", str(database), "init", "--backup-output", str(first_backup),
            "--confirm", "wrong",
        ])
    workforce_control_admin.main([
        "--database", str(database), "init", "--backup-output", str(first_backup),
        "--confirm", "INITIALIZE-WORKFORCE-CONTROL",
    ])
    initialized = json.loads(capsys.readouterr().out)
    assert initialized["runtime"]["mode"] == "paused"
    assert initialized["runtime"]["kill_switch"] == 1
    assert first_backup.stat().st_mode & 0o777 == 0o600

    shadow_backup = tmp_path / "backups" / "before-shadow.db"
    workforce_control_admin.main([
        "--database", str(database), "set-mode", "--mode", "shadow",
        "--reason", "isolated test", "--backup-output", str(shadow_backup),
        "--confirm", "ENABLE-WORKFORCE-SHADOW",
    ])
    shadow = json.loads(capsys.readouterr().out)
    assert shadow["runtime"]["mode"] == "shadow"
    assert shadow["runtime"]["kill_switch"] == 0
    assert shadow["backup"]["integrity"] == "ok"

    workforce_control_admin.main([
        "--database", str(database), "set-mode", "--mode", "paused",
        "--reason", "test complete", "--confirm", "PAUSE-WORKFORCE",
    ])
    paused = json.loads(capsys.readouterr().out)
    assert paused["runtime"]["mode"] == "paused"
    assert paused["runtime"]["kill_switch"] == 1
    assert paused["backup"] is None
