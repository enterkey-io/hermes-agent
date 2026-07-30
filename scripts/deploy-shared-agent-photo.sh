#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source_dir="$repo_root/skills/media/agent-photo"
shared_root="${HERMES_SHARED_SKILLS_DIR:-/home/elliott/.hermes/shared-skills}"
target_dir="$shared_root/agent-photo"
profiles_dir="${HERMES_PROFILES_DIR:-/home/elliott/.hermes/profiles}"
launcher_root="${HERMES_AGENT_PHOTO_BIN_DIR:-/home/elliott/.local/bin}"
launcher_path="$launcher_root/hermes-agent-photo"
fixed_python="/home/elliott/.hermes/hermes-agent/venv/bin/python"
fixed_runbook="/home/elliott/hermes-runbooks/scripts/run-hermes-agent-photo.py"
archive_local=false
snapshot_stage=""
launcher_stage=""
snapshot_mode=""
launcher_mode=""

atomic_exchange() {
  "$fixed_python" - "$1" "$2" <<'PY'
import ctypes
import os
import sys

AT_FDCWD = -100
RENAME_EXCHANGE = 2
libc = ctypes.CDLL(None, use_errno=True)
renameat2 = libc.renameat2
renameat2.argtypes = [
    ctypes.c_int,
    ctypes.c_char_p,
    ctypes.c_int,
    ctypes.c_char_p,
    ctypes.c_uint,
]
renameat2.restype = ctypes.c_int
if renameat2(
    AT_FDCWD,
    os.fsencode(sys.argv[1]),
    AT_FDCWD,
    os.fsencode(sys.argv[2]),
    RENAME_EXCHANGE,
) != 0:
    errno = ctypes.get_errno()
    raise OSError(errno, os.strerror(errno))
PY
}

publish_staged_snapshot() {
  local staged="$1"
  local target="$2"
  if [[ -e "$target" ]]; then
    atomic_exchange "$staged" "$target"
    printf 'exchange\n'
  else
    mv -T "$staged" "$target"
    printf 'create\n'
  fi
}

rollback_published_snapshot() {
  local staged="$1"
  local target="$2"
  local mode="${3:-exchange}"
  if [[ "$mode" == "exchange" ]]; then
    atomic_exchange "$staged" "$target"
  elif [[ "$mode" == "create" && -e "$target" ]]; then
    mv -T "$target" "$staged"
  fi
}

verify_snapshot_tree() {
  "$fixed_python" - "$source_dir" "$1" "$(id -u)" <<'PY'
import hashlib
import os
import stat
import sys
from pathlib import Path

source = Path(sys.argv[1])
snapshot = Path(sys.argv[2])
expected_uid = int(sys.argv[3])
file_modes = {
    "SKILL.md": 0o644,
    "requirements.txt": 0o644,
    "references/photo-prompting-rules.md": 0o600,
    "scripts/generate.py": 0o755,
    "scripts/identity_parser.py": 0o755,
    "scripts/prompt_profiles.py": 0o644,
}

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

if stat.S_IMODE(snapshot.stat().st_mode) != 0o755:
    raise SystemExit("snapshot root mode mismatch")
if snapshot.stat().st_uid != expected_uid:
    raise SystemExit("snapshot root owner mismatch")
actual_files = {
    str(path.relative_to(snapshot))
    for path in snapshot.rglob("*")
    if path.is_file()
}
if actual_files != set(file_modes):
    raise SystemExit(
        f"snapshot file set mismatch: actual={sorted(actual_files)!r}"
    )
if any(path.is_symlink() for path in snapshot.rglob("*")):
    raise SystemExit("snapshot symlink refused")
for directory in (snapshot / "references", snapshot / "scripts"):
    metadata = directory.stat()
    if metadata.st_uid != expected_uid or stat.S_IMODE(metadata.st_mode) != 0o755:
        raise SystemExit("snapshot directory metadata mismatch")
for relative, mode in file_modes.items():
    source_path = source / relative
    target_path = snapshot / relative
    metadata = target_path.stat()
    if (
        metadata.st_uid != expected_uid
        or stat.S_IMODE(metadata.st_mode) != mode
        or digest(source_path) != digest(target_path)
    ):
        raise SystemExit(f"snapshot verification failed: {relative}")
PY
}

verify_snapshot_runtime() {
  env -i \
    HOME=/home/elliott \
    PATH=/usr/bin:/bin \
    LANG=C.UTF-8 \
    "$fixed_python" - "$fixed_runbook" "$1" <<'PY'
import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("agent_photo_deploy_contract", sys.argv[1])
if spec is None or spec.loader is None:
    raise SystemExit("runbook wrapper unavailable")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.verify_photo_runtime_contract(
    Path(sys.argv[2]),
    source_environment={
        "HOME": "/home/elliott",
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
    },
)
PY
}

write_staged_launcher() {
  local destination="$1"
  printf '#!/bin/sh\nunset OP_SERVICE_ACCOUNT_TOKEN OP_USER AUTH_TOKEN CT0 GEMINI_API_KEY NOVITA_API_KEY XAI_API_KEY\nexec %s %s "$@"\n' \
    "$fixed_python" "$fixed_runbook" >"$destination"
  chmod 0755 "$destination"
}

verify_launcher() {
  "$fixed_python" - "$1" "$(id -u)" <<'PY'
import os
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
expected_uid = int(sys.argv[2])
expected = (
    "#!/bin/sh\n"
    "unset OP_SERVICE_ACCOUNT_TOKEN OP_USER AUTH_TOKEN CT0 "
    "GEMINI_API_KEY NOVITA_API_KEY XAI_API_KEY\n"
    "exec /home/elliott/.hermes/hermes-agent/venv/bin/python "
    "/home/elliott/hermes-runbooks/scripts/run-hermes-agent-photo.py \"$@\"\n"
)
metadata = path.lstat()
if (
    not stat.S_ISREG(metadata.st_mode)
    or metadata.st_uid != expected_uid
    or stat.S_IMODE(metadata.st_mode) != 0o755
    or path.read_text(encoding="utf-8") != expected
):
    raise SystemExit("launcher verification failed")
PY
}

handle_profile_shadows() {
  mapfile -d '' local_skills < <(
    find "$profiles_dir" -type f -path '*/skills/*/agent-photo/SKILL.md' \
      ! -path '*/skills/.archive/*' \
      ! -path '*/skills/.curator_backups/*' \
      -print0 2>/dev/null
  )

  if (( ${#local_skills[@]} > 0 )) && [[ "$archive_local" != true ]]; then
    printf 'Profile-local agent-photo skills still shadow the shared package:\n' >&2
    printf '  %s\n' "${local_skills[@]}" >&2
    printf 'Rerun with --archive-local after confirming rollback storage.\n' >&2
    return 2
  fi

  if [[ "$archive_local" == true ]]; then
    local timestamp
    timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
    for skill_md in "${local_skills[@]}"; do
      local skill_dir profile_dir relative_dir archive_dir
      skill_dir="${skill_md%/SKILL.md}"
      profile_dir="${skill_dir%%/skills/*}"
      relative_dir="${skill_dir#"$profile_dir/skills/"}"
      archive_dir="$profile_dir/skills/.archive/shared-agent-photo-$timestamp/$relative_dir"
      mkdir -p "$(dirname "$archive_dir")"
      mv "$skill_dir" "$archive_dir"
      printf 'Archived local skill: %s -> %s\n' "$skill_dir" "$archive_dir"
    done
  fi

  local remaining
  remaining="$({
    find "$profiles_dir" -type f -path '*/skills/*/agent-photo/SKILL.md' \
      ! -path '*/skills/.archive/*' \
      ! -path '*/skills/.curator_backups/*' \
      -print 2>/dev/null
  } | wc -l)"
  if [[ "$remaining" -ne 0 ]]; then
    printf 'Deployment incomplete: %s active profile-local copies remain.\n' "$remaining" >&2
    return 3
  fi
}

main() {
  if [[ "${1:-}" == "--archive-local" ]]; then
    archive_local=true
  elif [[ $# -gt 0 ]]; then
    printf 'Usage: %s [--archive-local]\n' "$0" >&2
    return 64
  fi

  if [[ ! -f "$source_dir/SKILL.md" || ! -x "$fixed_python" || ! -f "$fixed_runbook" ]]; then
    printf 'Agent-photo deployment inputs are unavailable.\n' >&2
    return 1
  fi

  handle_profile_shadows
  mkdir -p "$shared_root" "$launcher_root"
  chmod 0755 "$shared_root" "$launcher_root"

  snapshot_stage="$(mktemp -d "$shared_root/.agent-photo.stage.XXXXXX")"
  launcher_stage="$(mktemp "$launcher_root/.hermes-agent-photo.stage.XXXXXX")"

  cleanup() {
    local status=$?
    trap - EXIT
    if [[ $status -ne 0 ]]; then
      if [[ -n "${launcher_mode:-}" ]]; then
        rollback_published_snapshot "$launcher_stage" "$launcher_path" "$launcher_mode" || true
      fi
      if [[ -n "${snapshot_mode:-}" ]]; then
        rollback_published_snapshot "$snapshot_stage" "$target_dir" "$snapshot_mode" || true
      fi
    fi
    if [[ -n "${snapshot_stage:-}" ]]; then
      rm -rf "$snapshot_stage"
    fi
    if [[ -n "${launcher_stage:-}" ]]; then
      rm -f "$launcher_stage"
    fi
    exit "$status"
  }
  trap cleanup EXIT

  cp -a "$source_dir/." "$snapshot_stage/"
  rm -rf "$snapshot_stage/.venv" "$snapshot_stage/scripts/__pycache__"
  find "$snapshot_stage" -type f -name '*.pyc' -delete
  chown -R "$(id -u):$(id -g)" "$snapshot_stage"
  find "$snapshot_stage" -type d -exec chmod 0755 {} +
  chmod 0644 \
    "$snapshot_stage/SKILL.md" \
    "$snapshot_stage/requirements.txt" \
    "$snapshot_stage/scripts/prompt_profiles.py"
  chmod 0600 "$snapshot_stage/references/photo-prompting-rules.md"
  chmod 0755 \
    "$snapshot_stage/scripts/generate.py" \
    "$snapshot_stage/scripts/identity_parser.py"

  verify_snapshot_tree "$snapshot_stage"
  verify_snapshot_runtime "$snapshot_stage"
  write_staged_launcher "$launcher_stage"
  verify_launcher "$launcher_stage"

  snapshot_mode="$(publish_staged_snapshot "$snapshot_stage" "$target_dir")"
  verify_snapshot_tree "$target_dir"
  verify_snapshot_runtime "$target_dir"

  launcher_mode="$(publish_staged_snapshot "$launcher_stage" "$launcher_path")"
  verify_launcher "$launcher_path"

  snapshot_mode=""
  launcher_mode=""
  printf 'Deployed and verified agent-photo snapshot: %s\n' "$target_dir"
  printf 'Installed and verified agent-photo launcher: %s\n' "$launcher_path"
  printf 'Verified: no active profile-local agent-photo shadows remain.\n'
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
