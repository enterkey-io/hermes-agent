---
id: rb-workforce-opportunity-evaluation
slug: workforce-opportunity-evaluation
title: Workforce Opportunity Evaluation
purpose: Convert concrete workforce observations into delegated decisions without treating discovery as approval to launch work.
owner_profile: aurora
status: draft
runtime:
  kind: hermes
  ref: workforce-opportunity-evaluation
schedules: []
steps:
  - step_key: capture-signal
    name: Capture source signal and evidence
    executor_profile: aurora
    approval_policy: none
  - step_key: select-investigation
    name: Decide whether investigation is warranted
    executor_profile: aurora
    approval_policy: delegated-authority
  - step_key: request-domain-facts
    name: Request facts and constraints from the relevant director
    executor_profile: aurora
    approval_policy: delegated-authority
  - step_key: assemble-packet
    name: Assemble explicitly requested material
    executor_profile: chloe
    approval_policy: explicit-aurora-assignment
  - step_key: vision-option
    name: Develop an optional broader alternative
    executor_profile: mel
    approval_policy: explicit-aurora-request
  - step_key: portfolio-decision
    name: Approve, reject, defer, or reroute within delegated authority
    executor_profile: aurora
    approval_policy: delegated-authority
  - step_key: reserved-escalation
    name: Escalate only the retained decision to Elliott
    executor_profile: aurora
    approval_policy: elliott-explicit
  - step_key: route-approved-work
    name: Create or update canonical Kanban work
    executor_profile: aurora
    approval_policy: approved-work-only
  - step_key: close-signal
    name: Record decision, rationale, evidence, and downstream task IDs
    executor_profile: aurora
    approval_policy: none
inputs:
  workforce_signal: required
  evidence_references: required
outputs:
  aurora_decision: required
  downstream_kanban_task_ids: optional
permitted_writes:
  - workforce signal Kanban record
  - approved downstream Kanban records
approval_rules:
  routine_investigation: aurora
  reserved_actions: elliott-explicit
  no_response: pause-only-the-gated-step
retry:
  max_attempts: 2
timeout:
  seconds: 86400
deduplication:
  strategy: source-signal-id
related:
  organization: organization.yaml
  kanban: canonical
---

# Workforce Opportunity Evaluation

This is an unscheduled draft. Capturing an observation never authorizes work.

Directors supply facts, constraints, effort, dependencies, risks, and their
domain recommendation. They do not review or veto Aurora. Chloe may assemble
facts only under an explicit Aurora assignment; she does not interpret,
prioritize, recommend, route, or decide. Mel may challenge assumptions and
return alternatives; she does not operate or launch implementation.

Aurora owns the portfolio decision. If Elliott's retained authority is needed,
the escalation states `Decision needed`, `Why now`, `Options`,
`Recommendation`, `Risk if delayed`, and `Deadline`. Missing Elliott input
pauses only that gated step. Safe unrelated work continues.
