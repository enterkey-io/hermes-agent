# Hermes Workforce Model Assignments

Status: **owner-approved trial, reconciled September 13, 2026**.

The canonical live machine-readable authority is
`/home/elliott/.hermes/inventory/workforce-model-assignments.yaml`. This tracked
`workforce/model-assignments.yaml` is its versioned source and must remain
semantically equivalent. Update and review both before changing the roster.

## Route rules

- Main, Telegram, Buzz, and API sessions use the agent assignment below.
- Buzz must not have a separate model override. Its primary route never uses
  GLM; the configured service/rate fallback is a separate exception.
- Matrix is explicitly `ollama-cloud/glm-5.2:cloud` at medium effort for every
  operational profile. GLM-5.2 is reserved for Matrix chat; GLM-5.3 is the
  configured service/rate fallback exception.
- Voice remains a separate platform override. Where currently configured it is
  `openai-codex/gpt-5.4-mini` at low effort. Chloe and Emma have no voice route yet.
- Smart approval review remains `openai-codex/gpt-5.6-terra` at xhigh effort,
  independent of the conversation model.
- Cron jobs remain independently pinned in each profile's `cron/jobs.json`.
  The managed proactive runbook pins routine reconciliation to Luna xhigh,
  Vision/leverage synthesis to Terra high, and the deep weekly goals review to
  Sol medium. Other Cron pins still require separate review.
- Kanban dispatch is single-owner: only the `main` profile may run the gateway
  dispatcher. Every other profile has gateway dispatch disabled, and automatic
  task decomposition is disabled fleet-wide. Durable fan-out must be an
  intentional, reviewed task graph, not a side effect of whichever gateway
  acquires the shared lock first.
- GPT-5.6 profiles do not carry a fixed `model.context_length`; Hermes resolves
  the provider/model limit automatically. Native Responses compaction is
  configured at 750,000 tokens. That value is a provider-native trigger, not a
  model-capacity cap or a guarantee that native compaction owns every local
  preflight boundary.

## Configured GLM-5.3 fallback

- Global and profile fallback chains use `ollama-cloud/glm-5.3:cloud` at high
  reasoning effort only for rate limits, upstream overload/server errors, and
  timeouts.
- Content policy, context/payload recovery, tool/protocol/format failures,
  authentication, billing, and client errors stay on their native recovery or
  terminal paths.
- This does not replace primary assignments, Matrix `glm-5.2:cloud`, Voice,
  smart approval review, context/cache policy, or independently pinned Cron
  jobs. Amy and Kourtnie's GLM-5.2 primary routes share the provider, so this is
  not independent resilience for an Ollama Cloud outage.

## Cost-aware subagents

Aurora, Grace, and the seven operational directors retain their approved main
models for judgment, conversation, and final synthesis. Their ephemeral
`delegate_task` children are pinned to `openai-codex/gpt-5.6-luna` at xhigh
effort. Children are leaf workers with a 16-iteration budget, at most two may
run concurrently, each has a ten-minute timeout, and summaries are capped at
8,000 characters. Nested orchestration and automatic MCP inheritance are off.

Use these children for bounded extraction, comparison, inventory, first-pass
research or drafting, and independent artifact review. The parent supplies a
complete evidence packet, continues useful work while the child runs, and
verifies the returned claims. Do not delegate a single tool call, a mechanical
command sequence, a user interaction, or a decision about strategy, priority,
taste, authority, spending, publication, credentials, or real-money risk.
Durable work still belongs to the named workforce owner in Hermes Kanban; an
ephemeral subagent is never the accountable owner.

Every operational profile routes bounded auxiliary work to a lower-cost model.
Background review, compression, curator, memory flush, Kanban decomposition,
MCP orchestration, monitoring, profile description, session search, skill-hub
selection, title generation, triage specification, TTS audio tags, and web
extraction use Luna xhigh. Vision uses Terra high. Task-specific timeout and
tool settings remain intact. When the auxiliary model differs from the parent,
Hermes uses the compact routed context supported by that task instead of paying
the primary-model rate for mechanical support work.

The frequent managed proactive cycles do not pay the primary-model rate merely
to list, compare, and reconcile bounded evidence. Chloe, Milena, each daily
department review, and Aurora's twice-daily portfolio cycle are pinned to Luna
xhigh. Mel's formal 10x response and Aurora's weekly leverage/factory review
are pinned to Terra high because those steps require stronger synthesis. The
weekly canonical-goals review stays on Sol medium; its weekday projection is
Luna xhigh.

## Current roster

| Agent | Runtime profile | Main, Telegram, Buzz, API | Matrix |
|---|---|---|---|
| Aurora | `aurora` | `gpt-5.6-sol`, medium | `glm-5.2:cloud`, medium |
| Grace | `grace` | `gpt-5.6-sol`, medium | `glm-5.2:cloud`, medium |
| Brenna | `brenna` | `gpt-5.6-luna`, xhigh | `glm-5.2:cloud`, medium |
| Milena | `milena` | `gpt-5.6-luna`, xhigh | `glm-5.2:cloud`, medium |
| Chloe | `chloe` | `gpt-5.6-sol`, low | `glm-5.2:cloud`, medium |
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
| Mel | `mel` | `glm-5.3:cloud`, medium | `glm-5.2:cloud`, medium |

Totals: 9 Sol, 10 Terra, 2 Luna, and 1 GLM assignment.

Emma uses Sol at low effort as a specialist assignment: Sol preserves the
creative judgment, voice fidelity, and direct taste feedback her role needs,
while low effort keeps routine design conversation responsive. Complex campaign
strategy can still be escalated explicitly rather than making every interaction
pay the director-level reasoning cost.

Chloe also uses Sol at low effort. Her executive-assistant role depends on
nuanced intent recognition, relational continuity, and concise judgment; low
effort keeps routine coordination economical while Sol avoids the flattening
and behavioral mismatch observed on Luna.

## Change procedure

1. Confirm Elliott's intended assignment and update the canonical live YAML
   registry plus its versioned source.
2. Back up every affected `config.yaml`, `AGENTS.md`, and Cron store.
3. Set the profile default and reasoning effort; keep Matrix explicitly at
   GLM medium; do not create a Buzz override.
4. Reconcile model statements embedded in profile instructions.
5. Restart only affected gateways and verify the effective configuration.
6. Review Cron model pins separately because changing a profile default does
   not change an existing scheduled job.
7. Apply and validate `delegation_policy` separately for every eligible parent;
   verify one read-only child canary before relying on the route.
8. Apply and validate the complete auxiliary policy; verify low-cost tasks
   resolve to Luna, vision resolves to Terra, and task-specific limits survive.
9. Verify all profiles have no stale fixed `model.context_length`, Hermes still
   resolves each active provider/model automatically, and eligible GPT-5.6
   profiles retain the reviewed native Responses trigger.
