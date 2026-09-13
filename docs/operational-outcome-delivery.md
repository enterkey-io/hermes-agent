# Operational Failure Outcomes

Operator-owned Cron jobs can declare `required_tool_dependencies` and
`failure_ownership` in their native job metadata.
Required tool calls are tracked by actual invocation and typed results. Exact
MCP tool names and the built-in `terminal` tool are supported. Terminal failures
are sticky for the run: repeating the same command cannot erase an earlier
failure because its referenced files or external state may have changed.
Pending, failed, interrupted, or missing required calls prevent healthy status,
even when the agent produces useful partial output. Finalized run health cannot
be changed by a detached worker completing later. A genuinely silent result
remains silent; failure intake still records its dependency health.

`required_tool_dependency_mode` is operator-owned metadata with two values:
`always` (the default) treats an uncalled required dependency as degraded;
`when_invoked` permits legitimate branches that do not call it, such as preserving
an existing note section. Invoked failed or pending calls still degrade the run
in either mode. Otherwise, a conditional run with missing observations records
`last_dependency_status: not_observed`, preserves normal artifact delivery, and
emits neither a dependency failure nor a recovery event. Recovery still requires
successful observed calls to every configured dependency. Ordinary model tools
and the jobs API cannot set this mode. Runbook schedule metadata can declare it;
refreshing a runbook that omits it preserves the existing operator setting.
With `terminal` and `when_invoked`, a workflow may legitimately make no terminal
call, but any observed backend error or unexplained nonzero exit fails the run.
An attempted required call rejected by argument parsing, executor middleware,
registry admission, execution capability, pickup scope, input validation, or
runtime budget is an observed failure rather than an uncalled conditional
branch. Exit-zero terminal results carrying the
host's conservative masked-failure detection also fail required dependency
health; ordinary terminal calls retain the advisory result and exit code.
Ordinary terminal results retain advisory notes for explicit non-error codes in
the grep, diff, and test command families. Required dependency health accepts
those codes only for simple `/bin` or `/usr/bin` executables whose status is
unambiguous; bare names, custom paths, PATH overrides, conditional chains, and
active redirections fail closed. Explanatory notes for signals and network
failures do not. The
runtime persists only the tool name and bounded failure class, never command
text or arguments.
Background launches remain pending because spawning a process does not prove
its eventual exit outcome; required workflows must use a foreground command.

Owned failures use the existing Kanban coordination request, technical owner,
director review, and bounded model-call budget. A reviewed reserved user action
leaves the incident unrepaired and does not grant that action. Verified recovery
requires the current failure episode's source-success evidence and director
acceptance. An investigation that exhausts its checkpoint or work budget enters
source review as incomplete using the existing reserve; it cannot invent a user
action, reset the budget, or claim repair.

## Returning An Outcome

Set `failure_ownership.return_outcome_to_origin: true` through the operator-owned
job configuration to opt in. This is not a model-tool or API-writable routing
field. At failure intake, the producer captures up to four concrete destinations
from the persisted job's ordinary delivery configuration. Local-only jobs have
no external return route. Existing incidents without this captured provenance
are not retroactively routed.

The gateway returns only a director-accepted structured action or a
director-accepted verified recovery. Incomplete investigations remain internal;
raw worker summaries, artifacts, and unreviewed blocks are not published.
The source intake event, original coordination acceptance, current episode,
current job routing, and exact execution profile are revalidated before claiming
delivery. A changed route is withheld rather than redirected. A missing profile
adapter never borrows another profile's adapter.

The existing profile `state.db` delivery outbox stores one
`operational_outcome` record per coordination event and route. A successful
adapter result must include a real platform message ID before acknowledgement.
Timeouts, restarts during sending, missing receipts, and ambiguous failures are
`uncertain` and never automatically replayed. Definite pre-delivery rejections
have bounded backoff and at most three attempts within 24 hours. Generic outbox
recovery and pruning do not replay or remove these protected receipts.

The existing gateway coordination tick pages terminal events, permits at most
one coordination job per profile, and sends at most eight notices per batch.
Intake provenance inspection is bounded to 16 MiB and 100,000 records, with a
64 KiB per-record limit; records outside that verification bound are withheld.
No extra agent turn, background service, or parallel outbox is created.

## Verification

Run `scripts/run_tests.sh tests/gateway/test_operational_outcomes.py` for real
temporary-profile coverage of intake, native Cron route resolution, source
review, gateway ticks, receipt persistence, concurrent claims, restart/timeout
handling, route revocation, and profile isolation. Deployment acceptance also
requires a real enabled-channel receipt; green unit tests are not proof of a
production delivery.
