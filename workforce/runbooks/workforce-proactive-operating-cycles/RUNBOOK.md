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
  max_iterations: 12
  tool_budget:
    max_calls: 8
    max_writes: 1
    max_detail_reads: 3
    max_list_items: 20
    allowed_tools:
    - kanban_list
    - kanban_show
    - kanban_complete
    - kanban_block
    - kanban_request_review
    - kanban_request_changes
    - kanban_comment
    - kanban_attachments
    - workforce_signal
    - runbook_list
    - runbook_search
    - runbook_get
    - runbook_validate
    - runbook_runs
schedules:
- id: chloe-factual-reconciliation
  name: workforce-chloe-factual-reconciliation
  profile: chloe
  schedule: 50 8,14 * * 1-5
  enabled: true
  deliver: buzz:<ROOM_UUID:admin>
  step_key: chloe_observe
  enabled_toolsets: [kanban, workforce, runbook, no_mcp]
- id: milena-executive-follow-through
  name: workforce-milena-executive-follow-through
  profile: milena
  schedule: 10 10,16 * * 1-5
  enabled: true
  deliver: buzz:<ROOM_UUID:executive-support>
  step_key: milena_reconcile
  enabled_toolsets: [kanban, workforce, runbook, no_mcp]
- id: product-outcome-review
  name: workforce-product-outcome-review
  profile: emily
  schedule: 10 9 * * 1-5
  enabled: true
  deliver: buzz:<ROOM_UUID:director-product>
  step_key: director_product
  enabled_toolsets: [kanban, workforce, runbook, no_mcp]
- id: agent-systems-outcome-review
  name: workforce-agent-systems-outcome-review
  profile: alina
  schedule: 15 9 * * 1-5
  enabled: true
  deliver: buzz:<ROOM_UUID:director-agent-systems>
  step_key: director_agent_systems
  enabled_toolsets: [kanban, workforce, runbook, no_mcp]
- id: operations-outcome-review
  name: workforce-operations-outcome-review
  profile: main
  schedule: 20 9 * * 1-5
  enabled: true
  deliver: buzz:<ROOM_UUID:director-operations>
  step_key: director_operations
  enabled_toolsets: [kanban, workforce, runbook, no_mcp]
- id: marketing-outcome-review
  name: workforce-marketing-outcome-review
  profile: bridgette
  schedule: 25 9 * * 1-5
  enabled: true
  deliver: buzz:<ROOM_UUID:director-marketing>
  step_key: director_marketing
  enabled_toolsets: [kanban, workforce, runbook, no_mcp]
- id: trading-outcome-review
  name: workforce-trading-outcome-review
  profile: xenia
  schedule: 30 9 * * 1-5
  enabled: true
  deliver: buzz:<ROOM_UUID:director-trading>
  step_key: director_trading
  enabled_toolsets: [kanban, workforce, runbook, no_mcp]
- id: finance-outcome-review
  name: workforce-finance-outcome-review
  profile: maggie
  schedule: 35 9 * * 1-5
  enabled: true
  deliver: buzz:<ROOM_UUID:director-finance>
  step_key: director_finance
  enabled_toolsets: [kanban, workforce, runbook, no_mcp]
- id: vision-alternatives-review
  name: workforce-vision-alternatives-review
  profile: mel
  schedule: 40 9 * * 1-5
  enabled: true
  deliver: buzz:<ROOM_UUID:director-vision>
  step_key: director_vision
  enabled_toolsets: [kanban, workforce, runbook, no_mcp]
- id: aurora-portfolio-reconciliation
  name: workforce-aurora-portfolio-reconciliation
  profile: aurora
  schedule: 0 10,15 * * 1-5
  enabled: true
  deliver: buzz:<ROOM_UUID:admin>
  step_key: aurora_portfolio
  enabled_toolsets: [kanban, workforce, runbook, no_mcp]
steps:
- step_key: chloe_observe
  name: Confirm factual workforce current state
  description: Observe Aurora-approved surfaces, gather fresh evidence, normalize exact duplicates, and record fact-only signals under this standing Aurora assignment. Do not recommend, prioritize, approve, route, or launch work.
  executor_profile: chloe
  runtime_kind: hermes
- step_key: milena_reconcile
  name: Reconcile executive decisions and follow-through
  description: Compare executive decisions, commitments, owners, checkpoints, and durable records. Correct routine bookkeeping within role and escalate judgment through Grace or Aurora.
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
  description: Develop useful alternatives and constraints for Aurora without approving, prioritizing, routing, assigning, or executing work.
  executor_profile: mel
  runtime_kind: hermes
- step_key: aurora_portfolio
  name: Reconcile the workforce exception portfolio
  description: Review qualified signals, outcome truth, blockers, failed verification, and portfolio displacement. Decide, defer, reject, or route only justified work within delegated authority.
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
  meaningful_exception: concise evidence-backed result through the configured Buzz destination
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

- Use only the per-job `kanban`, `workforce`, and `runbook` tools. MCP servers, terminal, files, code execution, browser/web research, delegation, and platform messaging are intentionally unavailable.
- The host runtime also denies task creation, dependency linking, unblocking, workforce handoff, attachments, and runbook mutation during these cycles; the prompt cannot enlarge that allowlist.
- Make at most eight tool calls total, including at most one write and at most three individual task/workflow detail reads. Prefer one filtered list or dashboard snapshot over repeated item-by-item discovery.
- Review at most twenty candidate records, limited to the executing role's department or explicit reporting scope and changed, failed, blocked, review, or qualified-signal state. Never enumerate the whole board or workforce.
- Do not inspect host services, processes, networks, system configuration, credentials, raw databases, conversation archives, unrelated departments, or underlying implementation files.
- Use the current job's previous checkpoint and exact existing task, workflow, outcome, or signal references when available. Do not rediscover stable facts on every firing.
- If the bounded evidence is insufficient, contradictory, or would require a broader search, do not broaden the run. Record at most one non-executing signal when the issue is material; otherwise return quiet success.

1. Read the selected step, managed workforce contract, role, authority, and reporting line. Stay inside that exact scope.
2. Establish the current state before planning or reporting using the bounded internal sources above. Inspect the canonical task record, recent execution evidence, and relevant Workflow Registry state. Include directly linked completed or archived work so finished work is not presented as pending, but never scan either archive broadly.
3. Establish the applicable approved goal. Use Elliott's canonical goals source only through an authorized official integration. Otherwise use an explicit, current goal reference already present in the work or director assignment. If goal evidence is absent or contradictory, do not infer strategy.
4. Identify at most one highest-value issue or safe next action after considering priority, capacity, dependencies, deadlines, and what it would displace.
5. If work is already complete, reconcile the existing record and evidence within authority; do not create a replacement task. If it is duplicated or superseded, update or link the canonical record rather than launching another path.
6. A failed verification leaves the business outcome open. Link or propose one remediation path; never report the outcome as successful.
7. Continue clear, routine, reversible work already inside the approved goal and role, then verify the result. Chloe and Mel must obey their narrower step restrictions and never turn observation or ideation into execution.
8. For a substantial opportunity, ambiguous request, missing goal, cross-boundary issue, or material displacement, record at most one deduplicated non-executing signal for Aurora. Do not create an execution graph.
9. Reserved actions remain reserved regardless of urgency or potential value.
10. If there is no material drift, verified outcome, blocker, failed verification, qualified signal, or owner decision, return `[SILENT]` followed by the workflow completion marker required by the runtime. The scheduler will suppress delivery after removing the marker.
11. Otherwise report only the goal served, fresh evidence, action taken, accountable owner and next checkpoint, and exact unresolved decision if one exists. Use only the Cron-configured Buzz destination; do not call a platform messaging tool or use a fallback.

Success means Elliott did not need to notice, reconcile, or supervise something the workforce could competently handle itself.
