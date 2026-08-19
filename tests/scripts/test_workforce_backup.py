import importlib.util
import os
from pathlib import Path


ROOT = Path(__file__).parents[2]
SPEC = importlib.util.spec_from_file_location("workforce_backup", ROOT / "scripts" / "workforce_backup.py")
module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(module)


def test_backup_excludes_secrets_and_restores_hashes(tmp_path):
    profiles = tmp_path / "profiles"
    profile = profiles / "agent"
    (profile / "memories").mkdir(parents=True)
    (profile / "skills" / "one").mkdir(parents=True)
    (profile / "AGENTS.md").write_text("instructions")
    (profile / "memories" / "MEMORY.md").write_text("memory")
    (profile / "skills" / "one" / "SKILL.md").write_text("skill")
    (profile / "skills" / "one" / ".env").write_text("NESTED_SECRET=value")
    (profile / "skills" / "one" / "auth.json").write_text('{"token":"nested"}')
    (profile / "memories" / "logs").mkdir()
    (profile / "memories" / "logs" / "secret.log").write_text("volatile")
    (profile / ".env").write_text("SECRET=value")
    (profile / "auth.json").write_text('{"token":"secret"}')
    (profile / "config.yaml").write_text(
        "model:\n  default: test\nproviders:\n  secret:\n    api_key: hidden\n"
    )
    backup = tmp_path / "backup"
    result = module.create_backup(profiles, backup)
    assert result["profile_count"] == 1
    assert stat_mode(backup) == 0o700
    assert stat_mode(backup / "workforce-profiles.tar") == 0o600
    scratch = tmp_path / "restore"
    verified = module.verify_backup(backup, scratch)
    assert verified["valid"] is True
    assert not (scratch / "profiles" / "agent" / ".env").exists()
    assert not (scratch / "profiles" / "agent" / "auth.json").exists()
    assert not (scratch / "profiles" / "agent" / "skills" / "one" / ".env").exists()
    assert not (scratch / "profiles" / "agent" / "skills" / "one" / "auth.json").exists()
    assert not (scratch / "profiles" / "agent" / "memories" / "logs").exists()
    safe = (scratch / "profiles" / "agent" / "config.non-secret.yaml").read_text()
    assert "default: test" in safe
    assert "hidden" not in safe


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777
