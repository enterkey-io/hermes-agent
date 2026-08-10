# Hermes upstream integration through 56dc01d904

Date: 2026-08-10
Task: t_3ff4a313

## Integration

- Source branch: `upgrade/hermes-2026-08-10-kanban` at `d44f216f4a11a7561c2ecd03cd230e2077063ea8`
- Review branch: `upgrade/hermes-2026-08-10-kanban-56dc01`
- Exact upstream target: `56dc01d904d5826957208450e62a1634b5dc76a3`
- Integration commit: `4a9f1250f3e846f6b9eb6f21c04e26e2406fcc3a`
- Strategy: two-parent merge in a separate worktree. The first parent preserves all 122 local commits unchanged; the second parent preserves the exact 31-commit upstream range and contributor authorship.
- The merge completed without conflict markers. The six files changed on both sides were `agent/conversation_loop.py`, `cron/scheduler.py`, `hermes_state.py`, `run_agent.py`, `tests/cron/test_cron_no_agent.py`, and `tests/test_tui_gateway_server.py`.

One local regression test needed adaptation after the merge. The background-process sanitization test now uses `OPENAI_API_KEY` for the internal force-prefix case. `TELEGRAM_BOT_TOKEN` is a platform credential and is intentionally rejected by the hardened internal-secret filter even when a force-prefixed copy is supplied. The production sanitizer was not weakened.

## Local patch survival audit

Verified by ancestry, commit inventory, source search, and focused tests:

- Visible conversation archive: exporter, systemd units, documentation, and archive tests remain present.
- Honcho: profile-bound clients, shutdown/flush behavior, memo identity, profile fallback, and gateway rewrite behavior remain present.
- Kanban and workflow registry/runbooks: creator wake, profile authorization, canonical tools, cron wiring, dashboard plugin, proposal handling, and legacy read-only tools remain present.
- Approvals: exact one-time provenance, non-bypassable plugin approvals, and cron/gateway isolation remain present.
- Cron: inference contracts, profile isolation, private state/locks, workflow outcomes, and upstream no-agent `.env` delivery behavior remain present.
- Gateway: platform routing, Matrix/Telegram/Voice customizations, Honcho media-tail rewriting, and upstream truncation recovery remain present.
- Profile behavior: `HERMES_HOME`-scoped config, Honcho isolation, profile routing, and session search behavior remain present.

## Capability activation audit

- `automatic-active`: read-file special-file/FIFO guards, Unicode filename recovery, pagination notes, positive process wait timeout validation, config-module reload during update migrations, optional-skill live-repo fallback, recoverable session rewind/truncation, prompt-cache compatibility, and desktop/TUI recovery changes.
- `ready-existing-auth`: no-agent cron delivery now loads the profile `.env`, so existing configured home-channel delivery can resolve normally. No new credential is required.
- `setup-recommended`: none.
- `decision-required`: none. No model, provider, channel, schedule, credential, billing, or external publication change was enabled.
- `not-applicable to runtime rollout`: upstream CI workflow maintenance and the readtool evaluation harness.

## Verification

- Focused upstream and integration Python suites: 37 files, 1,098 passed, 4 platform skips.
- Local patch survival Python suites: 20 files passed with 587 focused test groups before the broad `run_agent` file; the selected archive, Honcho, approvals, Kanban, cron, gateway, and profile tests all passed. The broad `tests/run_agent/test_run_agent.py` run had 242 passes and one environment-only failure because the development venv does not include the optional `anthropic` package. The approval provenance suites themselves passed in `tests/tools/test_request_tool_approval.py` and `tests/hermes_cli/test_plugins.py`.
- Desktop: TypeScript typecheck passed; `chat-messages.test.ts` 53/53 passed; `assert-dist-built.test.mjs` 7/7 passed.
- TUI: TypeScript typecheck passed; after the required `build:ink` prerequisite, 139 files and 1,544 tests passed with one skip.
- Git checks: no unresolved merge state, target commit is an ancestor of the review branch, `git diff --check` passed, and `git fsck --no-dangling` passed.
- Main-profile smoke: with `HERMES_HOME=/home/elliott/.hermes/profiles/alina`, the profile-scoped config loaded 92 top-level keys, all 89 gateway modules imported, and the affected cron, state, file, process, skills, and TUI gateway modules imported successfully.

No production service was restarted and no production branch was moved.

## Rollout

Human review is required before rollout.

1. Review `upgrade/hermes-2026-08-10-kanban-56dc01`, especially the six overlapping files and the process-registry test adaptation.
2. After approval, move the designated production upgrade branch to the reviewed commit using the repository's normal reviewed merge procedure.
3. Run the static gateway import smoke against the production profile.
4. Restart `hermes-gateway-alina.service` only during the approved rollout.
5. Send a real Telegram message through the Alina profile and verify the full inbound/reply round trip. `systemctl is-active` alone is not acceptance.

## Rollback

Pre-integration source commit: `d44f216f4a11a7561c2ecd03cd230e2077063ea8`.

If the post-restart message round trip fails, immediately restore the production branch to that commit, restart `hermes-gateway-alina.service`, and verify a second real message round trip. Do not debug against live traffic while the gateway is unhealthy.
