---
name: kanban-workflows
description: Execute one assigned Kanban lifecycle phase safely.
version: 1.0.0
author: Nous Research
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [kanban, lifecycle, handoff, workforce]
    category: devops
    requires_toolsets: [kanban]
environments:
  - kanban
---

# Kanban Workflows

Use this skill only when the dispatcher assigns a Kanban task whose context has
a non-null `lifecycle_type`. Legacy cards without that opt-in keep the classic
Kanban completion/review behavior and do not use this contract. The task card
is the durable authority; your response text is not a handoff.

## Start Every Run

1. Call `kanban_show` and read the task, all comments, parent results, prior
   attempts, complete event history, latest handoff, and `graph_status`.
2. Identify `current_phase`, `original_author`, manager, `implementer`,
   `technical_reviewer`, `intent_validator`, `activation_owner`,
   `closure_owner`, and `return_to`.
3. Confirm this phase and work class fit your generated org-chart authority.
   If they do not, use the stuck route; do not silently take another role.
4. Work only inside the assigned workspace unless the card explicitly grants a
   narrower additional target. Verify the phase's actual outcome.

## Choose Exactly One Terminal Outcome

- `done_and_handoff`: the assigned phase is verified. Call `kanban_handoff`
  with the next assignee/phase, summary, evidence, expected outcome, and recheck
  condition. A technical reviewer uses `kanban_pass_review` for PASS instead.
- `changes_needed`: technical or intent review found correctable defects. Call
  `kanban_request_changes` with concrete, reproducible corrections; it returns
  the same card to the recorded implementer.
- `stuck_and_escalate`: progress is unsafe or impossible. Call
  `kanban_handoff(next_phase="recovery", next_assignee=<generated stuck route>)`
  with the exact failed step, evidence, needed action, expected outcome, and
  recheck condition. Use `kanban_block` only when the durable card truly must
  wait instead of transferring to an internal owner.
- `complete`: only the recorded `closure_owner`, after required technical PASS,
  intent validation, activation, and live acceptance evidence, calls
  `kanban_complete`. Include final delivery and `metadata.live_evidence`.

## Operational Failures Are Work

A failed command, tool, test, write, deployment, or lifecycle transition is an
operational failure, not a final answer. Before terminal prose, take and verify
one durable, owned action:

- Retry or recover when it is safe and within your phase authority, then finish
  through the phase's normal lifecycle transition.
- During technical or intent review, use `kanban_request_changes` on this same
  card with concrete, reproducible corrections.
- If another internal owner must recover it, use `kanban_handoff` to the
  authorized stuck route with failure evidence, expected outcome, and recheck
  condition.
- If a real external dependency must wait, use `kanban_block` with the exact
  failed step and concrete reason.

Merely advising Elliott or the user that an error exists, or promising that
someone will follow up, is advisory-only, not durable, and may not end the
task. Do not bypass permission, spending, credential, publication,
irreversible-action, or role boundaries to recover: route or block through the
rules above.

## Canonical Same-Card Routes

- Software: implementer → `kanban_request_review` to Reese; Reese FAIL →
  `kanban_request_changes` to implementer; Reese PASS → `kanban_pass_review` to
  intent validator; validator → activation owner when needed, otherwise live
  acceptance/closure; activation owner → original author for live acceptance.
- Research/analysis: Sage or Iris → Emily; Emily → original author when the
  request originated outside Product. Do not add Reese unless the deliverable
  includes code or another technical artifact.
- Product design: Maya → Aurora or the named Product author; implementation
  requirements then enter the software route.
- Marketing specialist → Bridgette; cross-team work → Aurora. Publication,
  campaign launch, paid spend, and new external contact stop at retained
  approval.
- Brenna or Milena → Grace; cross-team commitments → Aurora.
- Local Hermes/Ubuntu activation → Alina. External servers, providers,
  deployed apps, domains, DNS, SSL, or shared production → Root.
- Oyku → Xenia. Finance operations/records → Maggie. Real-money action stays at
  the retained approval gate.
- Mel returns alternatives to Aurora and never chooses an implementation
  successor. Chloe returns facts to Aurora and never chooses priorities or
  recommendations.

## Invariants

- Never create a child for an ordinary lifecycle stage. Reassign the same card;
  children are only for genuinely parallel work or durable dependencies.
- Never complete another role's phase. Code QA is not intent review or live
  acceptance.
- Never ask Elliott to coordinate internal work. Route developer/Product
  specialists to Emily, Marketing to Bridgette, Oyku to Xenia, Brenna/Milena
  to Grace, local-host capability to Alina, external infrastructure/providers
  to Root, Finance to Maggie, and cross-team ownership conflicts to Aurora.
- Only a genuinely retained spending, credential/security, irreversible-loss,
  publication/new-contact, real-money, or strategy decision routes to Elliott.
- Never end with prose saying someone else must continue while marking the card
  complete. The transition itself must preserve the next owner and wake path.
- Never put secrets, credentials, raw PII, or private relationship context in
  task summaries, evidence, comments, or handoff events.

## Dependency Outcomes

New parent-to-child links require a successful parent outcome by default. A
completed parent with an explicit non-passing `metadata.verdict` does not
release that edge. Use `required_outcome="completion"` only for a diagnostic or
reporting child that intentionally consumes either a successful or failed
terminal result; it is not a release bypass.

`kanban_show.graph_status` is the authoritative connected-graph view. Check its
overall state, active and blocked work, failed success gates, next owner/action,
and automatic final-report state before deciding that a workflow has finished
or stalled. A failed review stays on the same card and uses
`kanban_request_changes`; never encode it as `kanban_complete(verdict="fail")`.

## Final Check

Before the terminal tool call, confirm: one same card; correct phase owner;
verified evidence; explicit next expected outcome; explicit recheck condition;
and no remaining work hidden behind a completion claim.
