# Runbook Registry Phase -1 Baseline

Captured: 2026-08-07

## Source Control

- Primary checkout: `/home/elliott/.hermes/hermes-agent`
- Implementation worktree: `/home/elliott/.hermes/worktrees/runbook-registry-inventory`
- Implementation branch: `feature/runbook-registry-inventory`
- Selected integration base: current accepted local fork commit
  `7e9909e18720d95948438387a4546e9500d009ac`
- Source branch at capture: `fix/voice-suppress-runtime-output`
- Upstream divergence after `git fetch --multiple origin fork --prune`:
  - `origin/main...HEAD`: 2125 commits behind, 80 commits ahead
  - `fork/main...HEAD`: 12 commits behind, 10478 commits ahead

The active source checkout was intentionally left dirty and untouched. Dirty
paths at capture:

- `agent/agent_runtime_helpers.py`
- `agent/think_scrubber.py`
- `cli.py`
- `gateway/platforms/base.py`
- `gateway/platforms/voice.py`
- `gateway/stream_consumer.py`
- `tests/gateway/test_media_spaced_paths_and_history_dedupe.py`
- `tests/test_voice_adapter_filters.py` (untracked)

## Runtime

- Python: 3.12.3
- Hermes package version: 0.19.0
- pytest: 9.0.2
- Local test venv with pytest: `/home/elliott/.hermes/hermes-agent/.venv`

## Baseline Tests

Command:

```bash
scripts/run_tests.sh tests/cron/test_jobs.py tests/cron/test_scheduler.py tests/test_web_server.py tests/docker/test_dashboard.py tests/gateway/test_kanban_notifier.py
```

Result:

- `tests/test_web_server.py`: passed, 5 tests
- `tests/cron/test_jobs.py`: passed, 142 tests
- `tests/gateway/test_kanban_notifier.py`: passed, 17 tests
- `tests/cron/test_scheduler.py`: passed, 240 tests
- `tests/docker/test_dashboard.py`: did not complete before manual
  termination after the rest of the baseline slice had passed

The dashboard docker test stall is recorded as inherited baseline behavior, not
as a regression from registry work.
