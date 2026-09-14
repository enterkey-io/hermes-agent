# Kanban worker lanes

A **worker lane** is a class of process that the kanban dispatcher can route tasks to. Each lane has an identity (the assignee string), a spawn mechanism, and a contract for what it must do with the task once spawned.

This page is the contract. It exists for two audiences:

- **Operators** picking which lanes to wire into a board (which profiles to create, which assignees to use).
- **Plugin / integration authors** wanting to add a new lane shape (a CLI worker that wraps Codex / Claude Code / OpenCode, a containerised review worker, a non-Hermes service that pulls tasks via the API).

If you're writing the worker code itself — the agent that runs *inside* a lane — the dispatcher force-loads the bundled `kanban-workflows` skill. The system prompt contains only a compact bootstrap pointing to that skill, so ordinary conversations do not pay for the worker procedure.

## The hierarchy

```text
Hermes Kanban  =  canonical task lifecycle + audit trail
Worker lane    =  implementation executor for one assigned card
Reviewer       =  independent technical gate before intent validation
GitHub PR      =  upstreamable artifact (optional, for code lanes)
```

Hermes Kanban owns lifecycle truth — `ready` → `running` → `review` / `blocked` / `done` / `archived`, plus optional same-card role phases. Worker lanes execute work but never own that truth; everything they do flows back through the kanban kernel via the `kanban_*` tools (or, for non-Hermes external workers, via the API). On lifecycle-enabled cards, technical review routes to intent validation rather than directly to `done`.

## What a lane provides

To be a kanban worker lane, an integration must provide three things:

### 1. An assignee string

The dispatcher matches `task.assignee` against either a Hermes profile name (the default lane shape) or a registered non-spawnable identifier (the plugin lane shape — see [Adding an external CLI worker lane](#adding-an-external-cli-worker-lane) below). Tasks whose assignee doesn't resolve are left on `ready` with a `skipped_nonspawnable` event so a board operator can fix them; they are not silently dropped or executed by an arbitrary fallback.

### 2. A spawn mechanism

For Hermes profile lanes, the dispatcher's `_default_spawn` runs `hermes -p <assignee> chat -q <prompt>` (or the equivalent module form when the `hermes` shim isn't on `$PATH`) inside the task's pinned workspace, with these env vars set:

| Variable | Carries |
|---|---|
| `HERMES_KANBAN_TASK` | the task id the worker is operating on |
| `HERMES_KANBAN_DB` | absolute path to the per-board SQLite file |
| `HERMES_KANBAN_BOARD` | board slug |
| `HERMES_KANBAN_WORKSPACES_ROOT` | root of the board's workspace tree |
| `HERMES_KANBAN_WORKSPACE` | absolute path to *this* task's workspace |
| `HERMES_KANBAN_RUN_ID` | the current run's id (for the lifecycle gate) |
| `HERMES_KANBAN_CLAIM_LOCK` | the claim lock string (`<host>:<pid>:<uuid>`) |
| `HERMES_PROFILE` | the worker's own profile name (for `kanban_comment` author attribution) |
| `HERMES_TENANT` | tenant namespace, if the task has one |

For non-Hermes lanes (registered via a plugin), the plugin supplies its own `spawn_fn` callable that gets `task`, `workspace`, and `board` and returns an optional pid for crash detection.

### 3. A lifecycle terminator

Every claim must end in exactly one of:

- `kanban_complete(summary=..., metadata=...)` — task succeeds, status flips to `done`.
- `kanban_handoff(...)` — the current lifecycle phase succeeds and the same card is reassigned to its next owner without becoming `done`.
- `kanban_request_review(summary=..., metadata=..., reviewer=...)` — same-card implementation is complete and enters first-class review; status flips to `review`. The dispatcher additionally loads `sdlc-review` unless `kanban.review_dispatch` is disabled.
- `kanban_pass_review(summary=..., evidence=...)` — independent technical QA passes and the same card moves to its recorded intent validator/original author. Correctable QA or intent failures use `kanban_request_changes` to return to the recorded implementer.
- `kanban_block(reason=...)` — task waits for human input, status flips to `blocked`. The dispatcher respawns when `kanban_unblock` runs.
- The worker process exits without a tool call. The kernel reaps it and emits `crashed` (PID died) or `gave_up` (consecutive-failure breaker tripped) or `timed_out` (max_runtime exceeded). This is the failure path; healthy workers don't end here.

The kanban kernel enforces that exactly one of these terminates each run. A worker that calls neither and exits normally is treated as crashed.

## Outputs and the review handoff

For code-changing tasks, pick the review model encoded by the task graph:

- **Same-card review:** call `kanban_request_review(summary=..., metadata=..., reviewer=...)`. The task enters `review` without touching block recurrence accounting. The dispatcher claims it with both `kanban-workflows` and `sdlc-review` by default. The reviewer calls `kanban_pass_review` to route a verified candidate to intent validation, calls `kanban_request_changes(reason=...)` to return actionable defects to the recorded implementer, or blocks only for a genuine external dependency.
- **Pre-created downstream review/QA/release card:** `kanban_show` lists child IDs; inspect those cards with `kanban_show(task_id=...)` before choosing the terminal action. When a child is the downstream review/QA/release phase, call `kanban_complete` on the implementation phase. It cannot promote until this parent is `done`/`archived`. Do not additionally request same-card review and never sticky-block the parent with `review-required:` — either choice strands or duplicates the downstream lane.
- **Human-only boards:** set `kanban.review_dispatch: false`. A task can then remain in `review` until a human approves it or uses `reopen-review`/the dashboard to return it to `ready`/`todo`.

Both review models carry their structured handoff on the lifecycle transition itself. Do not place secrets, tokens, or raw PII in `summary` or `metadata`; run rows are durable.

The force-loaded `kanban-workflows` skill covers same-card handoff, review, intent, activation, acceptance, and closure. `KANBAN_GUIDANCE` remains a short safety bootstrap.

## Logs and audit trail

The dispatcher writes per-task worker stdout/stderr to `<board-root>/logs/<task_id>.log`. Logs are auditable from kanban metadata:

- `task_runs` rows carry the `log_path`, exit code (where available), summary, and metadata.
- `task_events` rows carry every state transition (`promoted`, `claimed`, `heartbeat`, `completed`, `blocked`, `review_requested`, `review_passed`, `handoff_created`, `assigned`, `changes_requested`, `review_reopened`, `gave_up`, `crashed`, `timed_out`, `reclaimed`, `claim_extended`).
- `kanban_show` returns both, so a reviewer (or a follow-up worker) reading the task gets the full history without needing dashboard access.

The dashboard renders run history with summaries, metadata blocks, and exit-status badges. CLI users can run `hermes kanban tail <task_id>` to follow live, or `hermes kanban runs <task_id>` for the historical attempt list.

## Existing lane shapes

### Hermes profile lane (default)

The shape every kanban worker takes today: the assignee is a profile name, the dispatcher spawns `hermes -p <profile>`, force-loads `kanban-workflows` (plus `sdlc-review` for review runs), injects a compact `KANBAN_GUIDANCE` bootstrap, and exposes the `kanban_*` lifecycle tools. No setup beyond defining the profile.

When you create profiles for your fleet, choose names that match the *role* you want the orchestrator to route to. The orchestrator (when there is one) discovers your profile names via `hermes profile list` — there's no fixed roster the system assumes (the orchestrator side of the contract is part of the injected `KANBAN_GUIDANCE`).

### Orchestrator profile lane

A specialisation of the profile lane: an orchestrator is a Hermes profile whose toolset includes `kanban` but excludes `terminal` / `file` / `code` / `web` for implementation. Its job is decomposing a high-level goal into child tasks via `kanban_create` + `kanban_link` and stepping back. The orchestrator skill encodes the anti-temptation rules.

## Adding an external CLI worker lane

Wiring a non-Hermes CLI tool (Codex CLI, Claude Code CLI, OpenCode CLI, a local coding-model runner, etc.) as a kanban worker lane is *not yet a paved path*. The dispatcher's spawn function is pluggable (`spawn_fn` is a parameter on `dispatch_once`), and a plugin could register its own `spawn_fn` for a non-Hermes assignee, but the surrounding integration work — wrapping the CLI's exit code into `kanban_complete` / `kanban_block` calls, mapping the CLI's workspace/sandbox conventions onto the dispatcher's `HERMES_KANBAN_WORKSPACE` env, handling auth and per-CLI policy — is still per-integration design work.

If you're considering adding a CLI lane, open an issue describing the specific CLI and the workflow you're trying to enable. The contract above is the constraints any such lane must satisfy; the implementation shape (one plugin per CLI vs a generic CLI-runner plugin parameterised by config) is open.

The historical issue for this is [#19931](https://github.com/NousResearch/hermes-agent/issues/19931) and the closed-not-merged Codex-specific PR [#19924](https://github.com/NousResearch/hermes-agent/pull/19924) — those describe the original architecture proposal but didn't land a runner.

## Failure modes the dispatcher handles

So lane authors don't have to reimplement these:

- **Stale claim TTL** — a worker that claims and then never heartbeats / completes / blocks gets reclaimed after `DEFAULT_CLAIM_TTL_SECONDS` (15 min default) — but only if the worker process has actually died. A live worker (slow model spending 20+ min in one tool-free LLM call) gets the claim *extended* instead of killed; only a dead PID is reclaimed.
- **Crashed worker** — a worker whose host-local PID has vanished is detected by `detect_crashed_workers` and reaped; the task increments `consecutive_failures` and may auto-block when the breaker trips.
- **Run-level retry** — when a task is retried (post-block, post-crash, post-reclaim), the worker can use the `expected_run_id` parameter on terminating tools to fail fast if its own run was already superseded.
- **Per-task max runtime** — `task.max_runtime_seconds` hard-caps wall-clock time per run, regardless of PID liveness. Catches genuinely-deadlocked workers that the live-PID extension would otherwise keep running.
- **Stranded-task detection** — a ready task whose assignee never produces a claim within `kanban.stranded_threshold_seconds` (default 30 min) shows up in `hermes kanban diagnostics` as a `stranded_in_ready` warning. Severity escalates to error at 2x the threshold and critical at 6x. Catches typo'd assignees, deleted profiles, and down external worker pools in one signal — identity-agnostic, no per-board allowlist to curate.
- **Legacy review dependency deadlock** — a parent sticky-blocked with `review-required:` while one or more direct children remain dependency-gated in `todo` produces an immediate `review_dependency_deadlock` error. The diagnostic is read-only: it suggests completing the finished phase or unlinking the incorrect edge but never removes a user block automatically.

## Related

- [Kanban overview](./kanban) — the user-facing intro.
- [Kanban tutorial](./kanban-tutorial) — walkthrough with the dashboard open.
- [`kanban-workflows`](/docs/user-guide/skills/bundled/devops/devops-kanban-workflows) — the task-worker-only same-card lifecycle procedure.
- [`KANBAN_GUIDANCE`](https://github.com/NousResearch/hermes-agent/blob/main/agent/prompt_builder.py) — the compact system-prompt bootstrap for Kanban workers.
