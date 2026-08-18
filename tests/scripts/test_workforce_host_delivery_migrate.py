import json
from pathlib import Path
import stat

import pytest

from scripts import workforce_host_delivery_migrate as migrate


ROOMS = {
    "director-operations": "11111111-1111-4111-8111-111111111111",
    "director-trading": "22222222-2222-4222-8222-222222222222",
    "executive-support": "33333333-3333-4333-8333-333333333333",
}


def fixture_crontab() -> str:
    return "\n".join(
        [
            "SHELL=/bin/bash",
            "*/15 * * * * /home/elliott/.hermes/scripts/xenia-tradestation-token-refresh.sh >> /tmp/x 2>&1",
            "10 4 * * * /usr/bin/python3 /x/refresh_token.py || /x/hermes send --to telegram --quiet failed",
            "0 4 * * * /usr/bin/python3 /x/refresh_google_accounts.py || /x/hermes send --to telegram --quiet failed",
            "0 7 * * 1 bash /home/elliott/nanoclaw/scripts/shared/weekly-security-audit.sh >> /tmp/w 2>&1",
            "0 5 1 * * /home/elliott/nanoclaw/scripts/shared/cron-monthly-disk-review.sh >> /dev/null 2>&1",
            "5 0 * * * /home/elliott/nanoclaw/scripts/move-logs-to-daily.sh || /home/elliott/nanoclaw/scripts/shared/elliott-msg.sh --as root --to enterkey failed",
            "5 0 * * * /home/elliott/nanoclaw/scripts/archive-sessions.sh || /home/elliott/nanoclaw/scripts/shared/elliott-msg.sh --as root --to enterkey failed",
            "*/2 * * * * /usr/bin/python3 /home/elliott/nanoclaw/watcher/watcher.py >> /tmp/watcher 2>&1",
            "15 4 * * * /home/elliott/nanoclaw/scripts/shared/op-onecli-sync.sh >> /tmp/op 2>&1",
            "",
        ]
    )


def fixture_xenia() -> str:
    return """#!/bin/bash
set -euo pipefail
umask 077
notify_failure() {
  if HERMES_HOME="$PROFILE_ROOT" \\
    /x/hermes send \\
    --to telegram --quiet \\
    "failed"; then
    true
  fi
}
"""


def test_transform_is_complete_and_idempotent():
    first, changes = migrate.transform_crontab(fixture_crontab(), ROOMS)
    second, second_changes = migrate.transform_crontab(first, ROOMS)

    assert first == second
    assert len(changes) == 9
    assert len(second_changes) == 9
    assert "--to telegram" not in first
    assert "elliott-msg.sh" not in first
    assert first.count("WORKFORCE_BUZZ_ROOM_ID=") == 5
    assert first.count(f"buzz:{ROOMS['executive-support']}") == 2
    assert first.count(f"buzz:{ROOMS['director-operations']}") == 2


def test_xenia_transform_requires_room_and_removes_telegram():
    result = migrate.transform_xenia_script(fixture_xenia())
    assert 'WORKFORCE_BUZZ_ROOM_ID="${WORKFORCE_BUZZ_ROOM_ID:-}"' in result
    assert 'buzz:${WORKFORCE_BUZZ_ROOM_ID}' in result
    assert "--to telegram" not in result
    assert migrate.transform_xenia_script(result) == result


def test_room_map_rejects_missing_duplicate_and_invalid(tmp_path: Path):
    path = tmp_path / "rooms.yaml"
    path.write_text("director-operations: nope\n")
    with pytest.raises(ValueError):
        migrate.load_room_map(path)

    path.write_text("\n".join(f"{name}: {ROOMS['director-operations']}" for name in sorted(ROOMS)))
    with pytest.raises(ValueError):
        migrate.load_room_map(path)


def test_apply_writes_rollback_and_installs_candidate(tmp_path: Path, monkeypatch):
    xenia = tmp_path / "xenia.sh"
    xenia.write_text(fixture_xenia())
    xenia.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    rollback = tmp_path / "rollback"
    installed = {}

    def fake_run(args, **kwargs):
        assert args == ["crontab", "-"]
        installed["crontab"] = kwargs["input"]

    monkeypatch.setattr(migrate.subprocess, "run", fake_run)
    report = migrate.migrate(
        crontab_text=fixture_crontab(),
        xenia_path=xenia,
        rooms=ROOMS,
        apply=True,
        rollback_dir=rollback,
    )

    assert report["applied"] is True
    assert (rollback / "crontab.before").read_text() == fixture_crontab()
    assert (rollback / "xenia-tradestation-token-refresh.sh.before").read_text() == fixture_xenia()
    assert "--to telegram" not in xenia.read_text()
    assert "--to telegram" not in installed["crontab"]
    manifest = json.loads((rollback / "manifest.json").read_text())
    assert manifest["crontab"]["sha256"]


def test_apply_restores_xenia_when_crontab_install_fails(tmp_path: Path, monkeypatch):
    xenia = tmp_path / "xenia.sh"
    xenia.write_text(fixture_xenia())
    rollback = tmp_path / "rollback"

    def fail_run(*_args, **_kwargs):
        raise RuntimeError("install failed")

    monkeypatch.setattr(migrate.subprocess, "run", fail_run)
    with pytest.raises(RuntimeError):
        migrate.migrate(
            crontab_text=fixture_crontab(),
            xenia_path=xenia,
            rooms=ROOMS,
            apply=True,
            rollback_dir=rollback,
        )
    assert xenia.read_text() == fixture_xenia()
