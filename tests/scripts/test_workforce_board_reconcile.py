from pathlib import Path

from hermes_cli import kanban_db
from scripts.workforce_board_reconcile import reconcile


def test_reconciliation_is_read_only_complete_and_evidence_backed(tmp_path: Path):
    db = tmp_path / "kanban.db"
    conn = kanban_db.connect(db)
    canonical = kanban_db.create_task(conn, title="Same useful outcome", assignee="aurora")
    duplicate = kanban_db.create_task(conn, title="Same useful outcome", assignee="aurora")
    blocker = kanban_db.create_task(conn, title="Needs decision", assignee="aurora", initial_status="blocked")
    with kanban_db.write_txn(conn):
        conn.execute("UPDATE tasks SET created_at=1 WHERE id=?", (canonical,))
        conn.execute("UPDATE tasks SET created_at=2 WHERE id=?", (duplicate,))
        conn.execute("UPDATE tasks SET block_kind='needs_input' WHERE id=?", (blocker,))
    conn.close()
    before = db.read_bytes()

    report = reconcile(db)

    assert report["mutation_performed"] is False
    assert report["open_cards"] == 3
    assert db.read_bytes() == before
    by_id = {item["task_id"]: item for item in report["dispositions"]}
    assert by_id[canonical]["classification"] == "still_required"
    assert by_id[duplicate]["classification"] == "duplicate"
    assert by_id[duplicate]["target_task_id"] == canonical
    assert by_id[blocker]["classification"] == "external_blocker"
    assert all(item["evidence_references"] for item in report["dispositions"])
    assert all(item["mutation_performed"] is False for item in report["dispositions"])
