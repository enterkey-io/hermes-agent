from scripts.workforce_host_delivery_inventory import build


def test_host_inventory_redacts_commands_and_classifies_failure_route():
    report = build(
        "SHELL=/bin/bash\n"
        "# old job\n"
        "10 4 * * * HERMES_HOME=/home/elliott/.hermes/profiles/grace "
        "/bin/task >> /tmp/log 2>&1 || hermes send --to telegram 'failed'\n"
    )
    assert report["active_host_schedules"] == 1
    item = report["entries"][0]
    assert item["owner_profile"] == "grace"
    assert item["intended_failure_room"] == "executive-support"
    assert item["migration_required"] is True
    assert "failed" not in str(report)


def test_host_inventory_accepts_explicit_buzz_failure_route():
    report = build(
        "10 4 * * * HERMES_HOME=/home/elliott/.hermes/profiles/grace "
        "/bin/task || hermes send --to buzz:11111111-1111-1111-1111-111111111111 'failed'\n"
    )
    item = report["entries"][0]
    assert item["migration_required"] is False
    assert item["intended_failure_room"] is None
    assert set(item["current_failure_route_markers"]) == {"buzz", "hermes-send"}
