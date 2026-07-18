# Visible Conversation Archive Implementation Plan

**Goal:** Export readable per-profile daily conversations from Hermes session
databases without including internal execution records.

**Architecture:** Add a standalone script at the edge of Hermes. It reads each
profile database through SQLite read-only mode, applies deterministic filters
and rendering, and atomically writes profile-local Markdown. A systemd user
timer invokes the same command nightly.

**Tech stack:** Python 3 standard library, Hermes secret redactor, SQLite,
pytest, systemd user services.

---

### Task 1: Specify filtering and rendering with tests

**Files:**
- Create: `tests/scripts/test_export_visible_conversations.py`

1. Build temporary schema-compatible session databases.
2. Assert exclusion of tools, reasoning, system rows, observed rows, internal
   session sources, blanks, and compression summaries.
3. Assert inclusion and ordering of visible user/final-assistant messages.
4. Assert recovery of compacted originals without replay duplicates.
5. Assert structured-content placeholders, redaction, local dates, and
   deterministic owner-only output.
6. Run the test and confirm it fails because the exporter is absent.

### Task 2: Implement the standalone exporter

**Files:**
- Create: `scripts/export-visible-conversations.py`

1. Discover profiles or accept explicit profile filters.
2. Open `state.db` with SQLite `mode=ro` and a bounded busy timeout.
3. Normalize visible content and filter internal rows and sources.
4. Deduplicate compaction copies and group messages by local date/session.
5. Force-redact content and render readable Markdown.
6. Atomically write changed files with mode `0600` under mode `0700`
   directories.
7. Expose dry-run, date, profile, timezone, and output controls with concise
   machine-usable exit status.

### Task 3: Verify against tests and production data

1. Run the focused test module to green.
2. Run syntax and help smoke checks.
3. Dry-run every current profile and confirm no database failures.
4. Export one profile to a temporary directory and inspect only structural
   metadata, role/source counts, permissions, and absence of internal markers.
5. Run the full initial export and rerun it to prove idempotency.

### Task 4: Install nightly automation and document operations

**Files:**
- Create: `docs/visible-conversation-archive.md`
- Install: `~/.config/systemd/user/hermes-conversation-archive.service`
- Install: `~/.config/systemd/user/hermes-conversation-archive.timer`

1. Document boundaries, paths, manual commands, recovery, and removal.
2. Install a oneshot service and persistent nightly timer.
3. Run the service manually and inspect status/journal output.
4. Enable the timer without restarting gateways.
5. Commit source, tests, and documentation on the feature branch.
