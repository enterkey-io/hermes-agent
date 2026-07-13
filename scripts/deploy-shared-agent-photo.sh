#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source_dir="$repo_root/skills/media/agent-photo"
target_dir="${HERMES_SHARED_SKILLS_DIR:-/home/elliott/.hermes/shared-skills}/agent-photo"
profiles_dir="${HERMES_PROFILES_DIR:-/home/elliott/.hermes/profiles}"
archive_local=false

if [[ "${1:-}" == "--archive-local" ]]; then
  archive_local=true
elif [[ $# -gt 0 ]]; then
  printf 'Usage: %s [--archive-local]\n' "$0" >&2
  exit 64
fi

if [[ ! -f "$source_dir/SKILL.md" ]]; then
  printf 'Canonical skill not found: %s\n' "$source_dir" >&2
  exit 1
fi

mkdir -p "$target_dir"
rsync -a --delete \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  "$source_dir/" "$target_dir/"

printf 'Deployed agent-photo to %s\n' "$target_dir"

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
  exit 2
fi

if [[ "$archive_local" == true ]]; then
  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  for skill_md in "${local_skills[@]}"; do
    skill_dir="${skill_md%/SKILL.md}"
    profile_dir="${skill_dir%%/skills/*}"
    relative_dir="${skill_dir#"$profile_dir/skills/"}"
    archive_dir="$profile_dir/skills/.archive/shared-agent-photo-$timestamp/$relative_dir"
    mkdir -p "$(dirname "$archive_dir")"
    mv "$skill_dir" "$archive_dir"
    printf 'Archived local skill: %s -> %s\n' "$skill_dir" "$archive_dir"
  done
fi

remaining="$({
  find "$profiles_dir" -type f -path '*/skills/*/agent-photo/SKILL.md' \
    ! -path '*/skills/.archive/*' \
    ! -path '*/skills/.curator_backups/*' \
    -print 2>/dev/null
} | wc -l)"
if [[ "$remaining" -ne 0 ]]; then
  printf 'Deployment incomplete: %s active profile-local copies remain.\n' "$remaining" >&2
  exit 3
fi

printf 'Verified: no active profile-local agent-photo shadows remain.\n'
