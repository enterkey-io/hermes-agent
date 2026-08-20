# Hermes Workforce Model Assignments

Status: **owner-approved trial effective 2026-08-18**.

The machine-readable authority is `workforce/model-assignments.yaml`. Update
that file first whenever Elliott changes the roster.

## Route rules

- Main, Telegram, Buzz, and API sessions use the agent assignment below.
- Buzz must not have a separate model override and must never use GLM.
- Matrix is explicitly `ollama-cloud/glm-5.2:cloud` at medium effort for every
  operational profile. GLM is reserved for Matrix chat.
- Voice remains a separate platform override. Where currently configured it is
  `openai-codex/gpt-5.4-mini` at low effort. Chloe and Emma have no voice route yet.
- Smart approval review remains `openai-codex/gpt-5.6-terra` at xhigh effort,
  independent of the conversation model.
- Cron jobs remain independently pinned in each profile's `cron/jobs.json`.
  Do not infer or change a Cron model from this table.

## Cost-aware subagents

Aurora, Grace, and the seven operational directors retain their approved main
models for judgment, conversation, and final synthesis. Their ephemeral
`delegate_task` children are pinned to `openai-codex/gpt-5.6-luna` at xhigh
effort. Children are leaf workers with a 16-iteration budget, at most two may
run concurrently, and each has a ten-minute timeout.

Use these children for bounded extraction, comparison, inventory, first-pass
research or drafting, and independent artifact review. The parent supplies a
complete evidence packet, continues useful work while the child runs, and
verifies the returned claims. Do not delegate a single tool call, a mechanical
command sequence, a user interaction, or a decision about strategy, priority,
taste, authority, spending, publication, credentials, or real-money risk.
Durable work still belongs to the named workforce owner in Hermes Kanban; an
ephemeral subagent is never the accountable owner.

## Current roster

| Agent | Runtime profile | Main, Telegram, Buzz, API | Matrix |
|---|---|---|---|
| Aurora | `aurora` | `gpt-5.6-sol`, medium | `glm-5.2:cloud`, medium |
| Grace | `grace` | `gpt-5.6-sol`, medium | `glm-5.2:cloud`, medium |
| Brenna | `brenna` | `gpt-5.6-luna`, xhigh | `glm-5.2:cloud`, medium |
| Milena | `milena` | `gpt-5.6-luna`, xhigh | `glm-5.2:cloud`, medium |
| Chloe | `chloe` | `gpt-5.6-luna`, xhigh | `glm-5.2:cloud`, medium |
| Emily | `emily` | `gpt-5.6-sol`, medium | `glm-5.2:cloud`, medium |
| Sage | `sage` | `gpt-5.6-terra`, high | `glm-5.2:cloud`, medium |
| Iris | `iris` | `gpt-5.6-terra`, high | `glm-5.2:cloud`, medium |
| Sloane | `sloane` | `gpt-5.6-terra`, high | `glm-5.2:cloud`, medium |
| Maya | `maya` | `gpt-5.6-terra`, high | `glm-5.2:cloud`, medium |
| Reese | `reese` | `gpt-5.6-terra`, high | `glm-5.2:cloud`, medium |
| Morgan | `morgan` | `gpt-5.6-terra`, high | `glm-5.2:cloud`, medium |
| Alina | `alina` | `gpt-5.6-sol`, medium | `glm-5.2:cloud`, medium |
| Root | `main` | `gpt-5.6-sol`, medium | `glm-5.2:cloud`, medium |
| Bridgette | `bridgette` | `gpt-5.6-terra`, high | `glm-5.2:cloud`, medium |
| Margot | `margot` | `gpt-5.6-terra`, high | `glm-5.2:cloud`, medium |
| Kenzie | `kenzie` | `gpt-5.6-terra`, high | `glm-5.2:cloud`, medium |
| Emma Calder | `emma` | `gpt-5.6-sol`, low | `glm-5.2:cloud`, medium |
| Xenia | `xenia` | `gpt-5.6-sol`, medium | `glm-5.2:cloud`, medium |
| Oyku | `oyku` | `gpt-5.6-terra`, high | `glm-5.2:cloud`, medium |
| Maggie | `maggie` | `gpt-5.6-sol`, medium | `glm-5.2:cloud`, medium |
| Mel | `mel` | `gpt-5.6-terra`, high | `glm-5.2:cloud`, medium |

Totals: 8 Sol, 11 Terra, and 3 Luna assignments.

Emma uses Sol at low effort as a specialist assignment: Sol preserves the
creative judgment, voice fidelity, and direct taste feedback her role needs,
while low effort keeps routine design conversation responsive. Complex campaign
strategy can still be escalated explicitly rather than making every interaction
pay the director-level reasoning cost.

## Change procedure

1. Confirm Elliott's intended assignment and update the YAML registry.
2. Back up every affected `config.yaml`, `AGENTS.md`, and Cron store.
3. Set the profile default and reasoning effort; keep Matrix explicitly at
   GLM medium; do not create a Buzz override.
4. Reconcile model statements embedded in profile instructions.
5. Restart only affected gateways and verify the effective configuration.
6. Review Cron model pins separately because changing a profile default does
   not change an existing scheduled job.
7. Apply and validate `delegation_policy` separately for every eligible parent;
   verify one read-only child canary before relying on the route.
