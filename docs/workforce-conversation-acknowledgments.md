# Contextual acknowledgments

The workforce contract asks agents to acknowledge addressed, multi-step human
requests briefly before starting work. A useful acknowledgment names the actual
concern and next action. It is not a generic receipt, tool log, reasoning trace,
or final promise that substitutes for execution.

Use Hermes' existing `display.platforms.<platform>.interim_assistant_messages`
setting for human text channels. An explicit `false` overrides Hermes' defaults
and disables the dedicated interim callback, including native structured Codex
commentary. It does not disable token streaming: a Chat Completions provider's
plain-text preamble can still arrive through the independent delta callback.
The gateway regression tests disable token streaming to isolate the interim
setting. Keep `show_reasoning` and `tool_progress` unchanged; neither enables
contextual assistant commentary. Do not broaden voice, webhook, or programmatic
delivery capabilities by changing a global setting merely to repair text chat.

For the maintained workforce, the text-channel rollout covers Telegram, Matrix,
Photon, and Buzz where configured. Preserve channel audience rules: this does
not authorize unsolicited room posts or private content in shared rooms.
Internal reconciliation and Cron keep their silence contracts. Protected
coordination final returns retain their existing host-enforced buffering and
one-return delivery claim.

The native provider adapter delivers user-visible commentary through the
existing interim callback and gateway stream consumer, independently of tool
logs and token streaming. There is no additional inference request, tool, timer,
or background workflow. Delivery is asynchronous: test visibility while work
is ongoing rather than claiming the network send precedes every CPU instruction
of a tool. Provider latency before commentary remains observable and is not
fixed by a display flag.

Acceptance requires both layers: effective channel settings and native runtime
evidence that the model emits useful commentary and the gateway delivers it
before the long-work result. Configuration presence and prompt text alone do
not establish acceptance. Also check quick answers, queued turns, failed sends,
stale generations, reaction-only responses, and final-response preservation.
Keep model/provider calls separate from platform-send receipts in reports.
