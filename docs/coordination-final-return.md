# Final Returns at an Execution Limit

An accepted origin request retains its original conversation and a bounded
reserve for the final report. Exhausting that reserve must not create another
provider call, a new worker, a replacement request, or a claim of completion.

While a request remains active, lifecycle review transitions may append a
deduplicated `coordination_checkpoint` to the aggregation root. The legacy
notifier may consume only those intermediate checkpoint events for that root;
it must not claim either final-return event. The receipt-backed coordination
return path remains the sole owner of `coordination_return_pending` and
`coordination_guardrail_reached`, including their delivery and acknowledgment.

Dependency edges created by current writers require a successful parent outcome
unless explicitly marked `completion` for a diagnostic/reporting consumer. An
explicit failing completion verdict leaves a success edge closed and moves an
active origin request to the existing guardrail return. Historical edges are
migrated as `completion` so an upgrade does not silently reinterpret a live
graph.

When an origin request reaches a guardrail, the same native transaction marks
its idle, unclaimed aggregation root blocked and records the reason. It never
stops a running root, changes an unrelated task, or turns failed work into done.
Ordinary task blocking and owned-failure review rules are unchanged.

The final-return model should inspect the existing outcomes and report them.
An already-blocked root does not need another block or worker claim. Preserve
the final reserved call for the response rather than another tool round.

If a final-return provider attempt is denied, the conversation loop creates a
fixed incomplete-report response and uses its ordinary finalizer to persist
that response. Work and terminal-review budget exceptions still propagate to
their existing owners. No retry, fallback, summary call, or budget increase is
authorized by this handling.

Persistence is not delivery. The gateway still requires an exact assistant
receipt, the current request/event, a terminal root, the original destination,
and the existing one-send claim. Cancellation, stale authority, a missing
receipt, and uncertain transport outcomes must not be bypassed or resent.
