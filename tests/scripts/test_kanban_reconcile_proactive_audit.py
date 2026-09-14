from __future__ import annotations

from hermes_cli import kanban_db
from scripts import kanban_reconcile_proactive_audit as audit


def test_audited_unfinished_archive_overwrites_stale_success_snapshot(tmp_path):
    conn = kanban_db.connect(tmp_path / "kanban.db")
    try:
        task_id = kanban_db.create_task(conn, title="Stale audited work")
        conn.execute(
            "UPDATE tasks SET terminal_outcome='success', terminal_verdict='pass' "
            "WHERE id=?",
            (task_id,),
        )

        audit._archive_audited_unfinished_task(conn, task_id)

        assert kanban_db.task_terminal_outcome(conn, task_id) == {
            "outcome": "failure",
            "verdict": None,
            "terminal": True,
        }
    finally:
        conn.close()
