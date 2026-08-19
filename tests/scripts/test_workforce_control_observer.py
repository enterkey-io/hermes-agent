from pathlib import Path

from hermes_cli import kanban_db
from plugins.workforce_control.store import runtime_state, set_runtime_mode
from scripts.workforce_control_observer import run


def test_recovery_observer_is_inert_when_paused(tmp_path, monkeypatch):
    database = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    with kanban_db.connect_closing(database) as conn:
        runtime_state(conn)

    assert run(database) == {
        "available": True,
        "paused": True,
        "processed": 0,
        "proposed": 0,
    }


def test_recovery_observer_advances_shared_cursor_in_shadow(tmp_path, monkeypatch):
    database = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    with kanban_db.connect_closing(database) as conn:
        runtime_state(conn)
        set_runtime_mode(conn, mode="shadow", kill_switch=False, reason="test")

    result = run(database)
    assert result["available"] is True
    assert result["paused"] is False
    assert result["processed"] == 0
    assert result["cursor"] == 0
    with kanban_db.connect_closing(database) as conn:
        row = conn.execute(
            "SELECT last_event_id FROM wc_cursors WHERE observer='dispatch_tick'"
        ).fetchone()
        assert row["last_event_id"] == 0
