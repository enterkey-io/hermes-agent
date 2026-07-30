from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).parents[2]
DEPLOY = REPO_ROOT / "scripts/deploy-shared-agent-photo.sh"
SOURCE = REPO_ROOT / "skills/media/agent-photo"
FIXED_PYTHON = "/home/elliott/.hermes/hermes-agent/venv/bin/python"
FIXED_RUNBOOK = "/home/elliott/hermes-runbooks/scripts/run-hermes-agent-photo.py"


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
        and ".venv" not in path.parts
    }


def test_deploy_publishes_complete_snapshot_and_fixed_launcher(tmp_path):
    shared = tmp_path / "shared-skills"
    profiles = tmp_path / "profiles"
    bin_dir = tmp_path / "bin"
    profiles.mkdir()
    environment = {
        **os.environ,
        "HERMES_SHARED_SKILLS_DIR": str(shared),
        "HERMES_PROFILES_DIR": str(profiles),
        "HERMES_AGENT_PHOTO_BIN_DIR": str(bin_dir),
        "OP_SERVICE_ACCOUNT_TOKEN": "must-not-appear",
        "GEMINI_API_KEY": "must-not-appear",
        "AUTH_TOKEN": "must-not-appear",
        "CT0": "must-not-appear",
    }

    result = subprocess.run(
        [str(DEPLOY)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    target = shared / "agent-photo"
    assert _tree_hashes(target) == _tree_hashes(SOURCE)
    assert target.stat().st_uid == os.getuid()
    launcher = bin_dir / "hermes-agent-photo"
    assert launcher.read_text(encoding="utf-8") == (
        "#!/bin/sh\n"
        "unset OP_SERVICE_ACCOUNT_TOKEN OP_USER AUTH_TOKEN CT0 "
        "GEMINI_API_KEY NOVITA_API_KEY XAI_API_KEY\n"
        f"exec {FIXED_PYTHON} {FIXED_RUNBOOK} \"$@\"\n"
    )
    metadata = launcher.stat()
    assert metadata.st_uid == os.getuid()
    assert stat.S_IMODE(metadata.st_mode) == 0o755
    combined_output = result.stdout + result.stderr
    assert "must-not-appear" not in combined_output

    refusal = subprocess.run(
        [str(launcher), "--preview-prompt", "test"],
        check=False,
        capture_output=True,
        text=True,
        env={
            **environment,
            "HERMES_HOME": str(tmp_path / "not-an-authorized-profile"),
        },
        timeout=30,
    )
    assert "must-not-appear" not in refusal.stdout + refusal.stderr


def test_atomic_exchange_rollback_restores_previous_snapshot(tmp_path):
    target = tmp_path / "agent-photo"
    staged = tmp_path / "agent-photo.stage"
    target.mkdir()
    staged.mkdir()
    (target / "marker").write_text("old", encoding="utf-8")
    (staged / "marker").write_text("new", encoding="utf-8")

    command = (
        f"source {DEPLOY!s}; "
        f"publish_staged_snapshot {staged!s} {target!s}; "
        f"rollback_published_snapshot {staged!s} {target!s}"
    )
    result = subprocess.run(
        ["bash", "-c", command],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert (target / "marker").read_text(encoding="utf-8") == "old"
    assert (staged / "marker").read_text(encoding="utf-8") == "new"
    script = DEPLOY.read_text(encoding="utf-8")
    assert "rsync" not in script
    assert "RENAME_EXCHANGE" in script
    assert "trap cleanup EXIT" in script
    assert (
        'rollback_published_snapshot "$snapshot_stage" "$target_dir" '
        '"$snapshot_mode"'
    ) in script
