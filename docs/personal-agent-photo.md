# Personal Agent Photos

The native `agent_photo` tool is limited to canonical personal profiles whose
organization status is `friend` and which are not operational workforce agents.
It invokes the fixed administrator-installed `hermes-agent-photo` wrapper,
never a shell command supplied by the model. The wrapper owns credential
injection, Characters bindings, identity assets and output paths.

## Current Requests

A direct current user request can authorize one photo without a second human
approval. The native ingress captures authored text before upload, history and
other prompt enrichment. A dedicated classifier uses the configured approval
auxiliary route. It does not treat tool arguments, quoted text, prior messages,
background work or internal delegation as user authorization.

The resulting capability is profile/session/turn-bound, one-use and bound to
the exact tool arguments and provider chain. Control messages, run completion,
errors and cancellation revoke it. Duplicate or overlapping calls cannot
reuse it. Classification failure blocks without generation or a redundant
human prompt. Unsupported origins and requests without direct authorization
retain the existing fresh human-approval path.

## Provider Attempts

Gemini is the default. One failed Gemini attempt can fall back to one Grok
attempt within the same authorized call. Set `fallback_to_grok=false` when the
user requests Gemini only. Explicit Grok or Seedream selections make one
attempt without fallback; neither can enable `fallback_to_grok=true`.

The native tool never forwards the standalone script's broad
`--allow-fallback` flag, which includes other providers. Instead, each attempt
uses the fixed wrapper with exact single-provider arguments. Successful output
stops the chain, and no provider is retried. A generic attempt failure does
not establish whether a provider call occurred. Explicit wrapper refusal,
start errors, cancellation and unverified cleanup stop without fallback.

The generation chain retains a 360-second shared budget, with a 180-second
Gemini attempt cap and up to five seconds of cleanup per stopped attempt.
The remaining budget limits Grok. On timeout or cancellation, the native tool
kills its isolated subprocess group, reaps the wrapper and verifies no live
group members remain before a timeout can permit fallback. It also checks the
captured run lifetime before each attempt, including human-approved calls
without a direct-request grant. Local cleanup cannot prove that a remote
provider did not finish a request; do not describe timeout as proof that no
image was generated.

## Image References

`references` lists image paths only inside the active profile's `assets`,
`baselines` and `media` directories. `characters_status` returns compact metadata
from the profile's exact Characters binding. These catalogs use `offset` and
`limit` (at most five entries), with `next_offset` for another page. The full
instruction action is not paginated or redirected to an unavailable file tool;
it includes the shared prompting rules.

`preview` and `generate` accept `source_images` and `characters_photo_ids`, with
at most four extra references combined. The input order is the identity seed,
Characters selections, then local selections. Local paths must remain inside the
allowed directories, traverse owner-controlled non-symlink directories, and name
regular owner-controlled PNG, JPEG or WebP images no larger than 25 MiB. The tool
verifies the bytes, binds their hashes to approval, and stages private snapshots
for the entire provider chain. A changed source invalidates an earlier approval.
The runner checks Characters ownership and places resolved source options before
the generator's prompt separator. The model never receives the app credential.

## Memory And Tool Selection

A personal profile's conversational toolsets must include `memory` to expose
Honcho's query tools. Configuring `memory.provider: honcho` alone initializes the
provider but does not override an explicit photo-only toolset. Include
`session_search` for private local transcript retrieval. Preserve existing
channel-specific selections and do not enable terminal or general file tools
merely to make photos or memory work.

For an already restricted conversation route, the relevant entries are:

```yaml
platform_toolsets:
  telegram:
    - agent_photo
    - memory
    - session_search
```

Apply the same additions to other explicitly restricted conversation routes as
appropriate, retaining their other toolsets. This is a configuration correction,
not a bypass of Hermes's memory-tool gate. Cron retains its separate toolset
resolution, disabled-memory policy and `skip_memory=True` initialization. Do not
reset sessions, replace Honcho peers, or rewrite archived history to restore
tool availability.

## Deployment Boundary

The maintained shared snapshot includes `characters_assets.py` and the ordered
Gemini multi-image path. The deployment manifest must retain those extensions
and the runner's required ownership and modes. Do not deploy an older snapshot
over a newer live extension. Validate the trusted wrapper and provider
credential injection separately, without generating paid media.
Deploy Hermes source through the maintained fork's normal protected release
and stock update path, then verify real user-requested generation and delivery.
