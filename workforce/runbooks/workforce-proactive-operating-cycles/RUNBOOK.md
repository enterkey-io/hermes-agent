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
schedules:
- id: chloe-factual-reconciliation
  name: workforce-chloe-factual-reconciliation
  profile: chloe
  schedule: 50 8,14 * * 1-5
  enabled: true
  deliver: buzz:<ROOM_UUID:admin>
  step_key: chloe_observe
- id: milena-executive-follow-through
  name: workforce-milena-executive-follow-through
  profile: milena
  schedule: 10 10,16 * * 1-5
  enabled: true
  deliver: buzz:<ROOM_UUID:executive-support>
  step_key: milena_reconcile
- id: product-outcome-review
  name: workforce-product-outcome-review
  profile: emily
  schedule: 10 9 * * 1-5
  enabled: true
  deliver: buzz:<ROOM_UUID:director-product>
  step_key: director_product
- id: agent-systems-outcome-review
  name: workforce-agent-systems-outcome-review
  profile: alina
  schedule: 15 9 * * 1-5
  enabled: true
  deliver: buzz:<ROOM_UUID:director-agent-systems>
  step_key: director_agent_systems
- id: operations-outcome-review
  name: workforce-operations-outcome-review
  profile: main
  schedule: 20 9 * * 1-5
  enabled: true
  deliver: buzz:<ROOM_UUID:director-operations>
  step_key: director_operations
- id: marketing-outcome-review
  name: workforce-marketing-outcome-review
  profile: bridgette
  schedule: 25 9 * * 1-5
  enabled: true
  deliver: buzz:<ROOM_UUID:director-marketing>
  step_key: director_marketing
- id: trading-outcome-review
  name: workforce-trading-outcome-review
  profile: xenia
  schedule: 30 9 * * 1-5
  enabled: true
  deliver: buzz:<ROOM_UUID:director-trading>
  step_key: director_trading
- id: finance-outcome-review
  name: workforce-finance-outcome-review
  profile: maggie
  schedule: 35 9 * * 1-5
  enabled: true
  deliver: buzz:<ROOM_UUID:director-finance>
  step_key: director_finance
- id: vision-alternatives-review
  name: workforce-vision-alternatives-review
  profile: mel
  schedule: 40 9 * * 1-5
  enabled: true
  deliver: buzz:<ROOM_UUID:director-vision>
  step_key: director_vision
- id: aurora-portfolio-reconciliation
  name: workforce-aurora-portfolio-reconciliation
  profile: aurora
  schedule: 0 10,15 * * 1-5
  enabled: true
  deliver: buzz:<ROOM_UUID:admin>
  step_key: aurora_portfolio
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

1. Read the selected step, managed workforce contract, role, authority, and reporting line. Stay inside that exact scope.
2. Establish the current state before planning or reporting. Inspect the canonical task record, recent execution evidence, relevant Workflow Registry state, and the underlying external system when it is available and authorized. Include completed and archived work so finished work is not presented as pending.
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
