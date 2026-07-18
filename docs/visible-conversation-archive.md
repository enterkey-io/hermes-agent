# Visible Conversation Archive

Hermes stores authoritative session history in each profile's `state.db`.
This host also produces a readable derivative archive at:

```text
~/.hermes/profiles/<profile>/conversations/daily/YYYY-MM-DD.md
```

## What Is Included

- Addressed user messages
- Final assistant text responses
- Compact image, audio, and file placeholders
- Direct messaging, voice, BlueBubbles, CLI, VOX/Photon, and imported NanoClaw
  conversation sources

## What Is Excluded

- System prompts and session metadata
- Tool calls, tool output, reasoning, and intermediate tool-call narration
- Observed group messages that were not addressed to the agent
- Cron, subagent, tool-only, and monitoring sessions
- Synthetic context-compaction summaries and duplicate replay rows
- Embedded data payloads and credential-shaped text

The archive is not `MEMORY.md`, Honcho, GBrain, or an agent input. It is a
human-readable recovery and inspection layer. `state.db` remains authoritative.

## Commands

Preview counts without writing:

```bash
~/.hermes/hermes-agent/scripts/export_visible_conversations.py --dry-run
```

Export all profiles:

```bash
~/.hermes/hermes-agent/scripts/export_visible_conversations.py
```

Export one profile or one local date:

```bash
~/.hermes/hermes-agent/scripts/export_visible_conversations.py --profile kenzie
~/.hermes/hermes-agent/scripts/export_visible_conversations.py --date 2026-07-17
```

Stage all output outside profile directories:

```bash
~/.hermes/hermes-agent/scripts/export_visible_conversations.py \
  --output-root /path/to/staging
```

Reruns rebuild each selected date deterministically and do not rewrite an
unchanged file. Existing archive dates are never automatically deleted when a
source session is pruned.

## Automation

The user timer runs nightly at approximately 2:35 AM America/Chicago:

```bash
systemctl --user status hermes-conversation-archive.timer
systemctl --user start hermes-conversation-archive.service
journalctl --user -u hermes-conversation-archive.service --since today
```

The service is independent of gateways. Failure cannot modify `state.db`, stop
an agent, or prevent messaging. Disable only the archive schedule with:

```bash
systemctl --user disable --now hermes-conversation-archive.timer
```

## Recovery And Verification

The command opens every source database with SQLite `mode=ro` and writes files
atomically with mode `0600` under a mode `0700` daily directory. It force-redacts
secrets even when normal Hermes redaction is disabled. Check a manual run's
exit status and journal summary before treating it as current.

The Markdown files are not automatically imported into a profile. For recovery,
restore `state.db` from backup when possible; use the daily archive only as a
readable source when database, Honcho, or migration recovery is unavailable.
