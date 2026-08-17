---
name: craft-work
description: Manage Elliott's Craft work through the reviewed broker.
version: 1.0.0
author: Elliott Hermes
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [productivity, craft, documents]
---

# Craft Work

Craft is Elliott's human-facing reference and work system. It is not agent
memory. Search before creating, use stable identifiers, and independently read
back every mutation.

## Authority

- Craft owns readable reference, planning, project, task, people, meeting,
  bookmark, specification, decision, and process records.
- Paperclip owns development execution: assignment, locks, dependencies,
  retries, QA, approvals, and agent runs. Craft stores the linked specification
  and verified outcome, not a duplicate execution lifecycle.
- GBrain provides source-scoped shared recall with provenance. It does not
  replace Craft authority or private memory; use `shared-brain` for it.
- Profile memory holds private continuity between Elliott and the agent.
- Obsidian, Todoist, and Microsoft To Do are read-only legacy evidence.

Read [authority.md](references/authority.md) whenever two systems overlap.

## Sanctioned Tool

Use only the reviewed local broker client:

```bash
node "$HOME/.hermes/shared-skills/craft-work/scripts/craft-ops-client.mjs" contracts
node "$HOME/.hermes/shared-skills/craft-work/scripts/craft-ops-client.mjs" run --contract <contract> --input '<json>'
node "$HOME/.hermes/shared-skills/craft-work/scripts/craft-ops-client.mjs" reconcile --run-id <run-id>
```

Read [tooling.md](references/tooling.md) before a command. A missing/failing
broker is a hard stop. Never use raw HTTP, a remote MCP URL, browser automation,
an alternate client, or direct credential access as fallback.

## Write Contract

1. Resolve an existing Craft ID, canonical URL, immutable source ID, or a
   reviewed domain key. A title or filename is never identity.
2. Search first and classify `absent`, `verified`, `conflict`, `uncertain`, or
   `drift`. Create only from `absent`.
3. Retain the run ID, idempotency marker, target ID, revision, and update
   preimage. Use that same run for resume, reconcile, or operator rollback.
4. Read back by stable ID and compare required fields, relations, provenance,
   and revision. A success response is not verification.
5. After timeout, partial failure, failed readback, `uncertain`, or `drift`, do
   not issue a fresh write. Reconcile the same run; resume only when its state
   permits it. Rollback is operator-only.

Read [identity-and-recovery.md](references/identity-and-recovery.md) for the
recovery state machine.

## Workflow Rules

- Meetings: Craft receives the organized record, decisions, owners, relations,
  and approved follow-ups. Native Craft tasks receive personal/lightweight
  actions; Paperclip receives development execution. Only selected reusable
  facts go to GBrain after Craft readback. Private relationship material stays
  in profile memory.
- Bookmarks: Craft Library is canonical. Deduplicate X, GitHub, web, and Readwise
  events by canonical URL, preserve every source ID/timestamp, and use a readable
  resolved title. GBrain may later ingest the verified Craft record; it is not
  the bookmark database.
- Missing process/spec: search Craft first. An Obsidian record is historical
  evidence, never provisional current authority. Surface the gap instead of
  silently promoting it.
- Development: read the current Craft spec/revision, dispatch through Paperclip,
  require separate QA where applicable, and write verified milestones/results
  back to the same Craft record.
- Daily Notes are native and date-scoped. Weekly reviews live under Personal
  Planning, not in a second top-level hierarchy.

Read [workflows.md](references/workflows.md) before executing one of these
workflows.
