# Visible Conversation Archive Design

## Goal

Create a durable, human-readable daily Markdown archive for every Hermes
profile using the authoritative conversation rows in that profile's
`state.db`. The archive supplements Hermes session search, Honcho, GBrain,
`MEMORY.md`, and `USER.md`; it does not replace or feed any of them.

## Content Boundary

Each daily file contains only conversation content a human would recognize:

- addressed user messages,
- final assistant text responses, and
- compact attachment placeholders when structured content has no text.

The exporter excludes system and session metadata, tool calls and results,
assistant reasoning, observed group chatter, automated cron runs, subagents,
monitoring/tool sessions, blank messages, and compression summaries. It reads
both live and durability-preserved compacted rows, then removes exact replay
duplicates so compaction cannot erase or multiply archived conversation.

## Storage

Files live under each profile at:

```text
$HERMES_HOME/conversations/daily/YYYY-MM-DD.md
```

Messages are grouped by source session and ordered by timestamp and database
row ID. Dates and display times use `America/Chicago`. Rows without a usable
timestamp go into `undated.md` rather than being mislabeled as 1970.

`state.db` remains authoritative. Output directories are owner-only, files are
written atomically with owner-only permissions, and credential-shaped text is
force-redacted before it reaches Markdown. A rerun deterministically rebuilds
the affected files and leaves byte-identical files untouched.

## Operation

A host-side Python command discovers profiles below `~/.hermes/profiles`, opens
each `state.db` read-only, and reports profile/file/message counts. It supports
profile and date filters plus dry-run mode for diagnosis. A systemd user timer
runs it nightly; failure affects only the derivative archive and never an
agent gateway or source database.

## Verification

Tests cover visible-role filtering, source filtering, observed messages,
tool-call and reasoning exclusion, compression-history recovery and
deduplication, summary removal, structured content, redaction, timezone date
grouping, deterministic reruns, read-only source access, atomic owner-only
files, and dry-run behavior. A production dry-run and an initial full export
must report no database or profile failures before enabling the timer.
