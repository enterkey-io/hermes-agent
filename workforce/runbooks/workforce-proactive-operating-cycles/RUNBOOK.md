---
id: wf_workforce_proactive_operating_cycles_v1
slug: workforce-proactive-operating-cycles
title: Workforce Proactive Operating Cycles
purpose: Wake the accountable workforce roles on a quiet cadence to reconcile current state, advance justified work, and surface only meaningful exceptions.
owner_profile: aurora
status: active
runtime:
  kind: hermes
  ref: workforce-control
  max_iterations: 8
  tool_budget:
    max_calls: 6
    max_writes: 1
    max_detail_reads: 2
    max_list_items: 12
    allowed_tools:
    - kanban_list
    - kanban_show
    - kanban_complete
    - kanban_block
    - kanban_request_review
    - kanban_request_changes
    - kanban_comment
    - kanban_archive_stale
    - kanban_attachments
    - workforce_signal
    - workforce_handoff
    - workforce_goals
    - workforce_vision
    - workforce_observe_buzz
schedules:
- id: chloe-factual-reconciliation
  name: workforce-chloe-factual-reconciliation
  profile: chloe
  schedule: 50 8,14 * * 1-5
  enabled: true
  deliver: local
  step_key: chloe_observe
  enabled_toolsets: [kanban, workforce, no_mcp]
  provider: openai-codex
  model: gpt-5.6-luna
  reasoning_effort: xhigh
- id: milena-executive-follow-through
  name: workforce-milena-executive-follow-through
  profile: milena
  schedule: 10 10,16 * * 1-5
  enabled: true
  deliver: local
  step_key: milena_reconcile
  enabled_toolsets: [kanban, workforce, no_mcp]
  provider: openai-codex
  model: gpt-5.6-luna
  reasoning_effort: xhigh
- id: product-outcome-review
  name: workforce-product-outcome-review
  profile: emily
  schedule: 10 9 * * 1-5
  enabled: true
  deliver: local
  step_key: director_product
  enabled_toolsets: [kanban, workforce, no_mcp]
  provider: openai-codex
  model: gpt-5.6-luna
  reasoning_effort: xhigh
- id: agent-systems-outcome-review
  name: workforce-agent-systems-outcome-review
  profile: alina
  schedule: 15 9 * * 1-5
  enabled: true
  deliver: local
  step_key: director_agent_systems
  enabled_toolsets: [kanban, workforce, no_mcp]
  provider: openai-codex
  model: gpt-5.6-luna
  reasoning_effort: xhigh
- id: operations-outcome-review
  name: workforce-operations-outcome-review
  profile: main
  schedule: 20 9 * * 1-5
  enabled: true
  deliver: local
  step_key: director_operations
  enabled_toolsets: [kanban, workforce, no_mcp]
  provider: openai-codex
  model: gpt-5.6-luna
  reasoning_effort: xhigh
- id: marketing-outcome-review
  name: workforce-marketing-outcome-review
  profile: bridgette
  schedule: 25 9 * * 1-5
  enabled: true
  deliver: local
  step_key: director_marketing
  enabled_toolsets: [kanban, workforce, no_mcp]
  provider: openai-codex
  model: gpt-5.6-luna
  reasoning_effort: xhigh
- id: trading-outcome-review
  name: workforce-trading-outcome-review
  profile: xenia
  schedule: 30 9 * * 1-5
  enabled: true
  deliver: local
  step_key: director_trading
  enabled_toolsets: [kanban, workforce, no_mcp]
  provider: openai-codex
  model: gpt-5.6-luna
  reasoning_effort: xhigh
- id: finance-outcome-review
  name: workforce-finance-outcome-review
  profile: maggie
  schedule: 35 9 * * 1-5
  enabled: true
  deliver: local
  step_key: director_finance
  enabled_toolsets: [kanban, workforce, no_mcp]
  provider: openai-codex
  model: gpt-5.6-luna
  reasoning_effort: xhigh
- id: vision-alternatives-review
  name: workforce-vision-alternatives-review
  profile: mel
  schedule: 40 9 * * 1,3,5
  enabled: true
  deliver: local
  step_key: director_vision
  enabled_toolsets: [kanban, workforce, no_mcp]
  provider: openai-codex
  model: gpt-5.6-terra
  reasoning_effort: high
- id: aurora-portfolio-reconciliation
  name: workforce-aurora-portfolio-reconciliation
  profile: aurora
  schedule: 0 10,15 * * 1-5
  enabled: true
  deliver: local
  step_key: aurora_portfolio
  enabled_toolsets: [kanban, workforce, no_mcp]
  provider: openai-codex
  model: gpt-5.6-luna
  reasoning_effort: xhigh
- id: aurora-leverage-factory-review
  name: workforce-aurora-leverage-factory-review
  profile: aurora
  schedule: 10 14 * * 4
  enabled: true
  deliver: local
  step_key: aurora_leverage
  enabled_toolsets: [kanban, workforce, no_mcp]
  provider: openai-codex
  model: gpt-5.6-terra
  reasoning_effort: high
steps:
- step_key: chloe_observe
  name: Confirm factual workforce current state
  description: Observe Aurora-approved surfaces, including a bounded recent window from Chloe's configured Buzz rooms, gather fresh evidence, normalize exact duplicates, and record fact-only signals under this standing Aurora assignment. Treat an accepted direct Elliott commitment with no acknowledgement, durable owner, checkpoint, or eventual final report in its originating conversation as observable friction. Do not recommend, prioritize, approve, route, or launch work.
  executor_profile: chloe
  runtime_kind: hermes
- step_key: milena_reconcile
  name: Reconcile executive decisions and follow-through
  description: Compare executive-support room outcomes, meeting and calendar workflow results, executive decisions, commitments, owners, checkpoints, originating-conversation return paths, and durable records. Correct routine bookkeeping within role and escalate judgment through Grace or Aurora. A start acknowledgement or internal handoff is not closure.
  executor_profile: milena
  runtime_kind: hermes
- step_key: director_product
  name: Reconcile Product outcome truth
  description: Review Product outcomes and safely advance the highest-value justified next action without inventing work.
  executor_profile: emily
  runtime_kind: hermes
- step_key: director_agent_systems
  name: Reconcile Agent Systems outcome truth
  description: Review Agent Systems outcomes, automation health, and reusable failure patterns; safely advance the highest-value justified next action.
  executor_profile: alina
  runtime_kind: hermes
- step_key: director_operations
  name: Reconcile Operations outcome truth
  description: Review operational outcomes and shared-system obligations; safely advance the highest-value justified next action.
  executor_profile: main
  runtime_kind: hermes
- step_key: director_marketing
  name: Reconcile Marketing outcome truth
  description: Review marketing outcomes, current strategy, and production evidence; safely advance the highest-value justified next action.
  executor_profile: bridgette
  runtime_kind: hermes
- step_key: director_trading
  name: Reconcile Trading outcome truth
  description: Review research and monitoring outcomes without placing trades, moving money, or implying execution approval; safely advance only authorized analysis.
  executor_profile: xenia
  runtime_kind: hermes
- step_key: director_finance
  name: Reconcile Finance outcome truth
  description: Review finance operations and evidence without spending, moving money, or making commitments; safely advance the highest-value authorized action.
  executor_profile: maggie
  runtime_kind: hermes
- step_key: director_vision
  name: Reconcile Vision alternatives
  description: Respond to at most one formal Aurora-requested Vision review with a reframe, a 10x alternative, assumptions, value case, risks, and the smallest test. Do not approve, prioritize, route, assign, or execute work.
  executor_profile: mel
  runtime_kind: hermes
- step_key: aurora_portfolio
  name: Reconcile the workforce exception portfolio
  description: Review qualified signals, outcome truth, blockers, failed verification, portfolio displacement, and accepted Elliott commitments missing a single return-to-origin aggregation card. Decide, defer, reject, or route only justified work within delegated authority; preserve one final-report obligation for every direct commitment.
  executor_profile: aurora
  runtime_kind: hermes
- step_key: aurora_leverage
  name: Find one leverage point or reusable factory
  description: Review current goals, repeated corrections, recurring runbook friction, and verified outcome patterns. Identify at most one valuable repeated bottleneck and either request one formal Vision review or quietly record that no qualified leverage point exists. Do not launch implementation.
  executor_profile: aurora
  runtime_kind: hermes
inputs:
  canonical_sources:
  - managed workforce contract and organization
  - Hermes Kanban including open, completed, and archived work
  - Workflow Registry and recent execution evidence
  - relevant external system when available and authorized
outputs:
  quiet_success: '[SILENT]'
  meaningful_exception: durable internal record or handoff; never a shared-room delivery
permitted_writes:
- routine reversible current-state corrections within the executing role
- one deduplicated non-executing workforce signal for a substantial or ambiguous issue
- verified progress on existing authorized work
approval_rules:
  prohibited:
  - speculative execution fanout
  - spending, trades, public publishing, new external commitments, credential changes, destructive action, or strategy changes without applicable approval
  - presenting unverified activity as a completed outcome
retry:
  max_attempts: 1
timeout:
  seconds: 1800
deduplication:
  strategy: schedule-and-evidence-window
related:
  workforce_managed: true
  goals_source: 'Evernote: Canonical Goals & Objectives: Elliott + Workforce'
---
# Procedure

This is a bounded proactive operating cycle, not permission to manufacture work.

## Hard evidence and execution budget

Every firing is an internal control-plane reconciliation, not a general research or systems-audit session.

- The complete canonical runbook and selected step are already present in this prompt. Do not call `tool_search`, `tool_describe`, or any `runbook_*` tool. Never call `workforce_reconcile`; it is not part of this bounded cycle. Use the named tools below directly, without spending calls rediscovering their schemas.
- Use only the per-job `kanban` and `workforce` tools. MCP servers, terminal, files, code execution, browser/web research, delegation, platform messaging, and runbook lookup tools are intentionally unavailable.
- The host runtime denies ordinary task creation, dependency linking, arbitrary unblocking, attachments, and runbook mutation during these cycles; the prompt cannot enlarge that allowlist. It permits one organization-authorized `workforce_handoff` when a real internal owner lacks a durable routed request, and stale archival only through `kanban_archive_stale`, which requires an exact cancellation/stop/supersession quote already stored on an inactive task.
- Make at most six tool calls total, including at most one write and at most two individual task/workflow detail reads. Prefer one filtered list or dashboard snapshot over repeated item-by-item discovery.
- Review at most twelve candidate records, limited to the executing role's department or explicit reporting scope and changed, failed, blocked, review, or qualified-signal state. Never enumerate the whole board or workforce.
- Do not inspect host services, processes, networks, system configuration, credentials, raw databases, conversation archives, unrelated departments, or underlying implementation files.
- Use the current job's previous checkpoint and exact existing task, workflow, outcome, or signal references when available. Do not rediscover stable facts on every firing.
- If the bounded evidence is insufficient, contradictory, or would require a broader search, do not broaden the run. Record at most one non-executing signal when the issue is material; otherwise return quiet success.

1. Read the selected step, managed workforce contract, role, authority, and reporting line. Stay inside that exact scope.
   - Make `workforce_goals` with `action: read` and `max_age_hours: 168` the first tool call. Chloe and Milena then call `workforce_observe_buzz`; Mel then calls `workforce_vision`; each department director then calls `kanban_list` once with `workforce_scope: owned_outcomes`, a relevant status when known, and `limit: 12`; Aurora uses `workforce_scope: portfolio_outcomes`. The host returns only decomposed root cards carrying durable explicit outcome-owner evidence; assignment alone never establishes ownership. Legacy roots without that evidence fail closed instead of being guessed from specialist assignments. A director may inspect child status through one returned canonical outcome but must not enumerate report-assigned cards. Never substitute another agent's name. Use `kanban_show` only for a record returned by that list, or for an exact task ID directly observed by Chloe or Milena in their bounded Buzz evidence. Chloe and Milena must not list the global blocked lane merely to rediscover an observed task ID. This order is mandatory so schema retries, scope drift, and tool discovery cannot consume the evidence budget.
2. Establish the current state before planning or reporting using the bounded internal sources above. Inspect the canonical task record, recent execution evidence, and relevant Workflow Registry state. Include directly linked completed or archived work so finished work is not presented as pending, but never scan either archive broadly. If the accountable owner, next checkpoint, blocker, and evidence are unchanged since the prior run, stop immediately with quiet success. Restating an existing blocked record is not progress or a meaningful exception.
3. Read the workforce-safe goal projection with `workforce_goals`. It is derived from Elliott's canonical Evernote note and is context, not a replacement source of truth. If it is missing or stale, use only an explicit current goal reference already present in the work or director assignment and signal a material alignment risk; never infer strategy.
4. Identify at most one highest-value issue or safe next action after considering priority, capacity, dependencies, deadlines, and what it would displace. An agent-authored suggestion for a sprint, interview, test, or further discovery is not a decision Elliott must make. Route it to the accountable manager as a recommendation.
5. If work is already complete, reconcile the existing record and evidence within authority; do not create a replacement task. If an inactive task has a direct Elliott/director stop, cancellation, or supersession statement in its own body, result, or comments, archive that existing record with `kanban_archive_stale`. Ambiguous evidence or a new strategic judgment must be signaled instead. If it is duplicated or superseded, update the canonical record rather than launching another path.
6. A failed verification leaves the business outcome open. Link or propose one remediation path; never report the outcome as successful.
   A direct Elliott commitment is not closed until the originating agent has delivered a final report in the exact originating conversation. If asynchronous work lacks one final `report_to_origin` aggregation card, Aurora repairs that bookkeeping without subscribing every internal child. Completion, a genuine retained decision, and an exhausted blocker all close the user-facing loop; silence does not.
7. Continue clear, routine, reversible work already inside the approved goal and role, then verify the result. Chloe and Mel must obey their narrower step restrictions and never turn observation or ideation into execution.
   - Chloe may call `workforce_observe_buzz` once for a bounded recent window from her configured rooms. She records only directly observed friction, contradictions, commitments, missing acknowledgements or final reports, or evidence that a task is already complete. Room conversation never becomes strategy or work merely because it was discussed.
   - Milena may call `workforce_observe_buzz` once for her configured executive-support room. She reconciles explicit decisions, meeting outcomes, commitments, owners, checkpoints, and final-report return paths against durable records. Discussion, suggestions, and autogenerated meeting summaries are not commitments unless the source language is explicit.
   - Mel lists pending `workforce_vision` reviews and responds to at most one. If Aurora has not requested a formal review, Mel returns quiet success instead of generating speculative ideas.
   - Aurora requests a Vision review only for one qualified, high-leverage outcome where a 10x reframe could materially change value or reveal a reusable factory. A request is not approval to execute the answer.
   - During the weekly leverage step, Aurora looks for a recurring high-value bottleneck, not a clever one-off. She must state the goal, repetition evidence, expected value, current workaround, and what a reusable primitive could replace before requesting Mel's review. If those facts are missing, return quiet success.
8. For a substantial opportunity, ambiguous request, missing goal, cross-boundary issue, or material displacement, record at most one deduplicated non-executing signal for Aurora. For a concrete internal dependency with no existing routed owner request, create at most one organization-authorized `workforce_handoff` to the manager or accountable agent with evidence, acceptance test, acknowledgment deadline, and checkpoint. Never duplicate an existing task or handoff.
9. Reserved actions remain reserved regardless of urgency or potential value. A blocked task or historical comment that says `human`, `owner`, or `Elliott` is not authority evidence. Before attributing a block to Elliott, prove that it matches an enumerated retained-approval category, is not covered by prior authorization, cannot use Aurora's signed routine-internal-repair review path, and has already been routed as one exact actionable request. Otherwise the blocker and route remain internal.
10. These schedules are internal control-plane workers and always deliver locally. After any durable correction, signal, handoff, or Vision response, return `[SILENT]` followed by the workflow completion marker required by the runtime. Do the same when there is nothing new. Never emit a narrative status report, agent-directed handoff, old blocker, or alleged Elliott approval request from this workflow.
11. Only Aurora or Grace, acting as the accountable escalation owner outside this internal workflow, may send Elliott a deduplicated request for a genuinely retained decision. That request must use the 30-second decision format and ask one explicit question. Everyone else routes internally through the reporting line.

Success means Elliott did not need to notice, reconcile, or supervise something the workforce could competently handle itself.
