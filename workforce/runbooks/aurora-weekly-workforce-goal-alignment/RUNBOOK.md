---
id: wf_workforce_goal_alignment_v1
slug: aurora-weekly-workforce-goal-alignment
title: Aurora Workforce Goal Alignment and Projection
purpose: Keep Elliott's canonical Evernote goals current, publish a bounded workforce-safe projection, and surface only meaningful alignment drift or owner decisions.
owner_profile: aurora
status: active
runtime:
  kind: hermes
  ref: profile:aurora
schedules:
- id: weekday-projection-0735
  name: aurora-weekday-workforce-goal-projection
  profile: aurora
  schedule: 35 7 * * 1-5
  timezone: America/Chicago
  enabled: true
  deliver: buzz:<ROOM_UUID:admin>
  step_key: publish
  enabled_toolsets: [workforce, mcp-evernote]
  max_iterations: 10
  provider: openai-codex
  model: gpt-5.6-luna
  reasoning_effort: xhigh
- id: weekly-friday-1630
  name: aurora-weekly-workforce-goal-alignment
  profile: aurora
  schedule: 30 16 * * 5
  timezone: America/Chicago
  enabled: true
  deliver: buzz:<ROOM_UUID:admin>
  step_key: review
  cron_job_id: c7c94a595fa0
  provider: openai-codex
  model: gpt-5.6-sol
  reasoning_effort: medium
steps:
- step_key: publish
  name: Publish the verified workforce-safe goals projection
  description: Read the exact canonical Evernote note and publish only the bounded goal fields operational agents need.
  executor_profile: aurora
  runtime_kind: hermes
  timeout_seconds: 900
  max_attempts: 1
- step_key: review
  name: Review workforce alignment against canonical goals
  description: Read and update the canonical Evernote goals source, publish its verified workforce-safe projection, compare it with active work and verified outcomes, and surface only material drift or decisions.
  executor_profile: aurora
  runtime_kind: hermes
  timeout_seconds: 3600
  max_attempts: 1
inputs:
  goals_source:
    system: Evernote
    title: 'Canonical Goals & Objectives: Elliott + Workforce'
    guid: f75d19af-3119-45b6-a72f-9103797d7569
    notebook: '0 Planning → _Vision & Goals'
  execution_sources:
  - Hermes Kanban
  - Workflow Registry
  - verified workflow outcomes and blockers
outputs:
  projection: Workforce Control goal snapshot with privacy-safe fields only
  primary: Buzz `admin` exception and alignment summary
permitted_writes:
- workforce-safe goal projection
- Hermes Kanban follow-up cards within Aurora's delegated authority during the weekly review
- updates to the canonical Evernote goals document that do not materially change Elliott-approved strategy
approval_rules:
  escalate:
  - material strategic-goal changes
  - spending or new commitments
  - credential, permission, or security changes
retry:
  max_attempts: 1
timeout:
  seconds: 3600
deduplication:
  strategy: exact-source-guid-update-and-content-hash
related:
  workforce_managed: true
  replaces: emily-weekly-goal-alignment
  source_job: aurora/weekly-canonical-goals-review
---
# Procedure

Elliott's Evernote note remains the only canonical goals source. The Workforce
Control snapshot is a small, privacy-safe operational projection, never another
goals document.

Canonical source: Evernote note `Canonical Goals & Objectives: Elliott +
Workforce`, GUID `f75d19af-3119-45b6-a72f-9103797d7569`, in notebook `0
Planning → _Vision & Goals`.

## Weekday projection step

1. Fetch exactly that GUID through the official Evernote MCP. Verify its exact
   title and notebook. Do not search for a substitute or use conversation
   memory when the read or identity check fails.
2. Extract only 1–24 explicit current goals. For each, publish `goal_id`,
   `title`, `desired_outcome`, `priority`, `status`, and relevant departments.
   Use a short stable ID derived from the note's own heading or label; preserve
   it on later unchanged runs. Omit private narrative, relationship context,
   raw note text, history, and supporting personal detail.
3. Call `workforce_goals` with action `publish`, the exact note GUID, title,
   provider-supplied update timestamp, and the bounded goal list. Read the
   snapshot back with action `read`; require the returned source identity,
   freshness, count, and content to match the projection just published.
4. If the verified note is unchanged, republish it anyway so freshness proves
   a successful daily source check. The control plane deduplicates the content.
5. On success return `[SILENT]`. On an identity, read, extraction, publish, or
   readback failure, report one concise exception through the Cron-configured
   Buzz `admin` destination; do not expose private note content and do not call
   a platform messaging tool.

## Weekly alignment step

1. Perform the weekday projection procedure first. If the canonical read fails,
   stop; stale or inferred goals cannot authorize alignment changes.
2. Review Elliott's authenticated decisions and relevant conversation history
   from the preceding seven days for clear changes involving his goals, free
   time, Enterkey income, trading or investing, LIFT transition, workforce
   purpose, current priorities, department focus, marketing direction, or
   approval boundaries.
3. Make only clear, directly supported updates to the existing Evernote note.
   Do not infer strategy, create another goals document, or add bureaucracy.
   Read the note back, verify it, and republish the bounded projection.
4. Compare verified goals with active and recently completed Hermes Kanban
   work, Workflow Registry schedules, outcomes, stalled commitments, and
   material decisions. Distinguish completed work from pending work and failed
   verification from a successful business outcome.
5. Correct routine alignment within Aurora's authority. Before creating work,
   search and reconcile existing active, completed, archived, duplicated, or
   superseded records. Create only the smallest justified path after the intake
   is execution-ready; never fan a tentative statement into a task graph.
6. Escalate only a material strategic, value, risk, cost, commitment, or
   retained-authority decision. Ask exactly one concise question when Elliott's
   decision is genuinely required.
7. If the note did not change and there is no material drift, blocker, action,
   or decision, return `[SILENT]`. Otherwise return one concise result through
   Buzz `admin`, without reproducing private personal detail.

Between weekly runs, Aurora updates the note and republishes the snapshot when
Elliott gives clear relevant direction. Never substitute a stale strategic
file, inferred priorities, conversation memory, Notion, or another notes system
for the verified canonical Evernote source.
