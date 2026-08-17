from __future__ import annotations

import hashlib
import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).parents[2]
DEPLOY = REPO_ROOT / "scripts/deploy-shared-agent-photo.sh"
SOURCE = REPO_ROOT / "skills/media/agent-photo"
FIXED_PYTHON = sys.executable


def _deployment_runtime(tmp_path: Path) -> dict[str, str]:
    runbook = tmp_path / "run-hermes-agent-photo.py"
    runbook.write_text(
        "def verify_photo_runtime_contract(*args, **kwargs):\n    return None\n",
        encoding="utf-8",
    )
    return {
        "HERMES_AGENT_PHOTO_PYTHON": FIXED_PYTHON,
        "HERMES_AGENT_PHOTO_RUNBOOK": str(runbook),
    }


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
        and ".venv" not in path.parts
    }


def _run_sourced_deploy(
    tmp_path: Path,
    shell_setup: str,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    shared = tmp_path / "shared-skills"
    profiles = tmp_path / "profiles"
    bin_dir = tmp_path / "bin"
    profiles.mkdir(exist_ok=True)
    command = "\n".join(
        (
            f"source {shlex.quote(str(DEPLOY))}",
            shell_setup,
            "main " + " ".join(shlex.quote(argument) for argument in arguments),
        )
    )
    return subprocess.run(
        ["bash", "-c", command],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            **_deployment_runtime(tmp_path),
            "HERMES_SHARED_SKILLS_DIR": str(shared),
            "HERMES_PROFILES_DIR": str(profiles),
            "HERMES_AGENT_PHOTO_BIN_DIR": str(bin_dir),
            "OP_SERVICE_ACCOUNT_TOKEN": "rollback-secret",
            "GEMINI_API_KEY": "rollback-secret",
            "AUTH_TOKEN": "rollback-secret",
            "CT0": "rollback-secret",
        },
        timeout=30,
    )


def _write_profile_photo_skill(profiles: Path, profile: str, marker: str) -> Path:
    skill = profiles / profile / "skills" / "media" / "agent-photo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(marker, encoding="utf-8")
    return skill


def test_deploy_publishes_complete_snapshot_and_fixed_launcher(tmp_path):
    shared = tmp_path / "shared-skills"
    profiles = tmp_path / "profiles"
    bin_dir = tmp_path / "bin"
    profiles.mkdir()
    environment = {
        **os.environ,
        **_deployment_runtime(tmp_path),
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
        f"exec {FIXED_PYTHON} {environment['HERMES_AGENT_PHOTO_RUNBOOK']} \"$@\"\n"
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
        env={
            **os.environ,
            **_deployment_runtime(tmp_path),
        },
    )

    assert result.returncode == 0, result.stderr
    assert (target / "marker").read_text(encoding="utf-8") == "old"
    assert (staged / "marker").read_text(encoding="utf-8") == "new"
    script = DEPLOY.read_text(encoding="utf-8")
    assert "rsync" not in script
    assert "RENAME_EXCHANGE" in script
    assert "trap cleanup EXIT" in script
    assert "if ! rollback_published_snapshot" in script
    assert script.index("trap cleanup EXIT") < script.index(
        "handle_profile_shadows\n"
    )


def test_failed_snapshot_rollback_preserves_prior_and_surfaces_unverified_live(
    tmp_path,
):
    shared = tmp_path / "shared-skills"
    target = shared / "agent-photo"
    target.mkdir(parents=True)
    (target / "marker").write_text("prior", encoding="utf-8")
    failure_marker = tmp_path / "fail-rollback"
    setup = f"""
eval "$(declare -f atomic_exchange | sed '1s/atomic_exchange/original_atomic_exchange/')"
atomic_exchange() {{
  if [[ -e {shlex.quote(str(failure_marker))} ]]; then
    return 91
  fi
  original_atomic_exchange "$@"
}}
verify_snapshot_tree() {{ return 0; }}
verify_snapshot_runtime() {{
  if [[ "$1" == "$target_dir" ]]; then
    : > {shlex.quote(str(failure_marker))}
    return 86
  fi
  return 0
}}
"""

    result = _run_sourced_deploy(tmp_path, setup)

    assert result.returncode != 0
    transaction = shared / ".agent-photo.transaction"
    prior = transaction / "snapshot"
    assert (prior / "marker").read_text(encoding="utf-8") == "prior"
    assert (target / "SKILL.md").is_file()
    assert stat.S_IMODE(transaction.stat().st_mode) == 0o700
    status_path = transaction / "STATUS"
    assert stat.S_IMODE(status_path.stat().st_mode) == 0o600
    status = status_path.read_text(encoding="utf-8")
    assert "rollback_status=failed" in status
    assert "live_status=unverified" in status
    assert f"live_path={target}" in status
    assert f"prior_path={prior}" in status
    combined = result.stdout + result.stderr
    assert "rollback_status=failed" in combined
    assert "live_status=unverified" in combined
    assert "rollback-secret" not in combined
    assert "restored" not in combined.lower()


def test_failed_launcher_rollback_blocks_snapshot_rollback(tmp_path):
    shared = tmp_path / "shared-skills"
    target = shared / "agent-photo"
    target.mkdir(parents=True)
    (target / "marker").write_text("prior-snapshot", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launcher = bin_dir / "hermes-agent-photo"
    launcher.write_text("prior-launcher", encoding="utf-8")
    launcher.chmod(0o755)
    failure_marker = tmp_path / "fail-launcher-rollback"
    setup = f"""
eval "$(declare -f atomic_exchange | sed '1s/atomic_exchange/original_atomic_exchange/')"
atomic_exchange() {{
  if [[ -e {shlex.quote(str(failure_marker))} && "$2" == "$launcher_path" ]]; then
    return 91
  fi
  original_atomic_exchange "$@"
}}
verify_snapshot_tree() {{ return 0; }}
verify_snapshot_runtime() {{ return 0; }}
verify_launcher() {{
  if [[ "$1" == "$launcher_path" ]]; then
    : > {shlex.quote(str(failure_marker))}
    return 86
  fi
  return 0
}}
"""

    result = _run_sourced_deploy(tmp_path, setup)

    assert result.returncode != 0
    snapshot_transaction = shared / ".agent-photo.transaction"
    launcher_transaction = bin_dir / ".hermes-agent-photo.transaction"
    assert (target / "SKILL.md").is_file()
    assert (
        snapshot_transaction / "snapshot" / "marker"
    ).read_text(encoding="utf-8") == "prior-snapshot"
    assert launcher.read_text(encoding="utf-8").startswith("#!/bin/sh\n")
    assert (
        launcher_transaction / "launcher"
    ).read_text(encoding="utf-8") == "prior-launcher"
    assert stat.S_IMODE(snapshot_transaction.stat().st_mode) == 0o700
    assert stat.S_IMODE(launcher_transaction.stat().st_mode) == 0o700
    assert "rollback_status=failed" in (
        launcher_transaction / "STATUS"
    ).read_text(encoding="utf-8")
    snapshot_status = (snapshot_transaction / "STATUS").read_text(
        encoding="utf-8"
    )
    assert "rollback_status=blocked" in snapshot_status
    assert "live_status=unverified" in snapshot_status
    assert "rollback-secret" not in result.stdout + result.stderr


def test_failure_after_profile_archival_restores_original_skill(tmp_path):
    profiles = tmp_path / "profiles"
    skill = _write_profile_photo_skill(profiles, "amy", "amy-prior")

    result = _run_sourced_deploy(
        tmp_path,
        "verify_snapshot_tree() { return 72; }",
        "--archive-local",
    )

    assert result.returncode != 0
    assert (skill / "SKILL.md").read_text(encoding="utf-8") == "amy-prior"
    assert not list(profiles.rglob(".archive/**/agent-photo/SKILL.md"))
    assert "archive_restore_status=failed" not in result.stderr


def test_partial_multi_profile_archive_failure_restores_every_moved_skill(tmp_path):
    profiles = tmp_path / "profiles"
    amy = _write_profile_photo_skill(profiles, "amy", "amy-prior")
    maggie = _write_profile_photo_skill(profiles, "maggie", "maggie-prior")
    move_count = tmp_path / "archive-move-count"
    setup = f"""
mv() {{
  local source="${{@: -2:1}}"
  if [[ "$source" == */skills/*/agent-photo ]]; then
    local count=0
    if [[ -f {shlex.quote(str(move_count))} ]]; then
      count="$(<{shlex.quote(str(move_count))})"
    fi
    count=$((count + 1))
    printf '%s' "$count" > {shlex.quote(str(move_count))}
    if [[ "$count" -eq 2 ]]; then
      return 73
    fi
  fi
  command mv "$@"
}}
"""

    result = _run_sourced_deploy(tmp_path, setup, "--archive-local")

    assert result.returncode != 0
    assert (amy / "SKILL.md").read_text(encoding="utf-8") == "amy-prior"
    assert (maggie / "SKILL.md").read_text(encoding="utf-8") == "maggie-prior"
    assert not list(profiles.rglob(".archive/**/agent-photo/SKILL.md"))


def test_archive_restore_collision_preserves_source_and_archive(tmp_path):
    profiles = tmp_path / "profiles"
    skill = _write_profile_photo_skill(profiles, "amy", "amy-prior")
    setup = f"""
verify_snapshot_tree() {{
  mkdir -p {shlex.quote(str(skill))}
  printf '%s' replacement > {shlex.quote(str(skill / "SKILL.md"))}
  return 74
}}
"""

    result = _run_sourced_deploy(
        tmp_path,
        setup,
        "--archive-local",
    )

    assert result.returncode != 0
    archives = list(profiles.rglob(".archive/**/agent-photo/SKILL.md"))
    assert len(archives) == 1
    assert archives[0].read_text(encoding="utf-8") == "amy-prior"
    assert (skill / "SKILL.md").read_text(encoding="utf-8") == "replacement"
    assert "archive_restore_status=failed" in result.stderr
    assert f"source_path={skill}" in result.stderr
    assert f"archive_path={archives[0].parent}" in result.stderr
    assert "rollback-secret" not in result.stdout + result.stderr
