# Hermes Upgrade Plan - 2026-06-11

## Goal

Upgrade Elliott's Hermes checkout from the stale local history to current
`origin/main` without losing local production customizations.

## Ground Rules

- Do not merge old local branches wholesale. They are based more than 2,000
  upstream commits behind and would roll back large parts of Hermes.
- Work in the isolated worktree:
  `/home/elliott/.config/superpowers/worktrees/hermes-agent/upgrade-hermes-2026-06-11`
- Keep the live checkout at `/home/elliott/.hermes/hermes-agent` untouched until
  the ported branch passes focused tests.
- Preserve current live dirty changes through named branches/commits before any
  live upgrade. Current Matrix changes are saved as
  `feature/matrix-coordination-and-mentions` commit `8b5172ba7`.

## Already Upstream or Superseded

- `fix/codex-null-output-stream` (`f590ac501`): upstream now reconstructs Codex
  output when terminal completed events have null output, with regression tests.
- XAI OAuth fixes (`b23ba1ce`, `cef968316`): upstream has profile/global auth
  fallback and entitlement-shaped 403 rotation handling.
- `feature/platform-model-overrides` (`eaf8f5915`): upstream has session model
  and reasoning override routing plus tests.

## Port Candidates

- Matrix coordination and mention aliases (`8b5172ba7`):
  - YAML/env bridge for Matrix allowlists, observe rooms, and mention aliases.
  - Coordination room observation and coordinator context injection.
  - Outbound alias mention conversion for agent names such as `@maya`.
  - Must be manually ported because upstream Matrix support has changed heavily.
- Vox voice platform:
  - Prefer upstream's plugin platform registry rather than old hardcoded gateway
    adapter insertion.
  - Preserve `VOICE_HOME_CHANNEL` cron delivery and voice allowlist behavior.
- ElevenLabs voice settings:
  - Port final behavior from `0b7ee8d2` + `de3ade5c`: pass only explicitly
    configured `VoiceSettings` values.
- Telegram MarkdownV2 italic paragraph break (`40525eef`):
  - Applies cleanly, but still needs focused Telegram formatting tests.
- Newline preservation:
  - Old changes to message splitting and stream fallback still appear relevant.
  - Port the whitespace-only trimming behavior with targeted tests.
- Voice TTS suppression (`15dc83752`):
  - Port only if Vox voice platform remains in scope.

## Deliberately Excluded Unless Requested

- `patch/gbrain-research` (`d6d539a22`): this vendors an entire GBrain project
  tree into Hermes. GBrain has its own repo at `/home/elliott/gbrain`, so it is
  not part of the Hermes upgrade unless explicitly requested.

## Verification Gates

- Baseline current-upstream tests in the worktree before custom ports:
  - `scripts/run_tests.sh tests/run_agent/test_run_agent_codex_responses.py`
  - `scripts/run_tests.sh tests/gateway/test_session_model_override_routing.py`
  - `scripts/run_tests.sh tests/gateway/test_telegram_format.py`
- After ports:
  - Matrix mention/coordination tests, including lowercase aliases.
  - Telegram formatting tests.
  - TTS tests for ElevenLabs kwargs without real API calls.
  - Stream/message newline preservation tests.
  - Voice platform registration/cron delivery tests if Vox is ported.

## Rollout

1. Build and test the ported upgrade branch in the worktree.
2. Preserve or tag the current live checkout state.
3. Update the live checkout to the tested upgrade branch.
4. Restart only affected Hermes user services after focused smoke tests.
5. Keep local feature branches available for future rebase/cherry-pick.
