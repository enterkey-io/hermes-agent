from scripts import workforce_environment_inventory as inventory


def test_active_crontab_excludes_environment_assignments(monkeypatch):
    fixture = """SHELL=/bin/bash
HOME=/home/elliott
# disabled
0 4 * * * /bin/job || hermes send --to telegram failed
"""
    monkeypatch.setattr(inventory, "_run", lambda _args: (0, fixture))
    result = inventory._active_crontab()
    assert result["active_lines"] == 1
    assert result["marker_counts"]["telegram"] == 1
    assert result["marker_counts"]["hermes send"] == 1
