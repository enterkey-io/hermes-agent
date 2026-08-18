import json
from pathlib import Path

from scripts.workforce_delivery_migrate import migrate


ROOT = Path(__file__).parents[2]


def test_delivery_migration_rewrites_team_route_without_touching_friend(tmp_path):
    profiles = tmp_path / "profiles"
    main_jobs = profiles / "main" / "cron" / "jobs.json"
    main_jobs.parent.mkdir(parents=True)
    main_jobs.write_text(json.dumps({"jobs": [{
        "id": "root-job", "name": "check", "enabled": True,
        "deliver": "telegram:123", "prompt": "Send the exception to Telegram."
    }]}))
    friend_jobs = profiles / "amy" / "cron" / "jobs.json"
    friend_jobs.parent.mkdir(parents=True)
    friend_jobs.write_text(json.dumps({"jobs": [{
        "id": "friend-job", "enabled": True, "deliver": "telegram:456"
    }]}))
    before_friend = friend_jobs.read_bytes()
    room_map = {
        "director-operations": "11111111-1111-1111-1111-111111111111"
    }
    report = migrate(
        profiles_root=profiles,
        organization=ROOT / "workforce" / "organization.yaml",
        policy=ROOT / "workforce" / "delivery-policy.yaml",
        topology=ROOT / "workforce" / "buzz-topology.yaml",
        room_map=room_map,
        apply=True,
    )
    assert report["applied"] is True
    updated = json.loads(main_jobs.read_text())["jobs"][0]
    assert updated["deliver"] == "buzz:11111111-1111-1111-1111-111111111111"
    assert "Telegram" not in updated["prompt"]
    assert "[WORKFORCE DELIVERY POLICY]" in updated["prompt"]
    assert friend_jobs.read_bytes() == before_friend
