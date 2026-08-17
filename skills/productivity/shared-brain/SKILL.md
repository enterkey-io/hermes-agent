---
name: shared-brain
description: Search Elliott's shared GBrain knowledge safely.
version: 1.0.0
author: Elliott Hermes
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [memory, gbrain, search]
---

# Shared Brain

GBrain is shared source-scoped recall. It is not Craft's replacement and it is
not any agent's private memory.

## Sources

- `shared_craft`: knowledge derived from verified Craft records. Use this first
  for current work, while treating Craft itself as final authority.
- `shared_meetings`: approved reusable meeting knowledge. Raw transcripts and
  private interpersonal details do not belong here. Facts selected from a
  verified Craft meeting record belong in `shared_craft`, not here.
- `shared_federated`: historical and other approved shared sources. Treat older
  material as evidence, not current policy.

Every request requires one explicit alias. Never omit the source or search a
private profile store. Read [authority.md](references/authority.md) when deciding
between Craft, GBrain, and profile memory.

## Sanctioned Tool

Use only the local read-only broker client:

```bash
node "$HOME/.hermes/shared-skills/shared-brain/scripts/gbrain-broker-client.mjs" sources --source shared_craft --params '{}'
node "$HOME/.hermes/shared-skills/shared-brain/scripts/gbrain-broker-client.mjs" search --source shared_craft --params '{"query":"...","limit":5}'
node "$HOME/.hermes/shared-skills/shared-brain/scripts/gbrain-broker-client.mjs" get --source shared_craft --params '{"page_ref":"..."}'
node "$HOME/.hermes/shared-skills/shared-brain/scripts/gbrain-broker-client.mjs" graph --source shared_craft --params '{"page_ref":"...","depth":1}'
```

Read [protocol.md](references/protocol.md) before using the client. Search
returns opaque short-lived `page_ref` values; only use those refs for `get` or
`graph`. Do not construct slugs, source IDs, paths, or socket overrides.

## Retrieval Contract

1. Choose the explicit source based on the question and search it.
2. Preserve source/provenance when using a result. A plausible hit is not
   authority if the current Craft record says otherwise.
3. Use `get` for the full bounded page and `graph` only when relationships are
   relevant. Stop on broker error; do not fall back to GBrain CLI, its database,
   raw files, HTTP, or MCP.
4. If current truth matters, verify against Craft before acting or writing.
5. Keep private relationship facts and agent-specific continuity in the
   profile's memory even when a shared fact from the same conversation belongs
   in GBrain.

For private continuity, follow the active profile's existing memory mechanism
and paths defined in its `AGENTS.md`; this skill does not authorize a new memory
store or arbitrary file writes. If no sanctioned profile-memory write path is
loaded, retain the detail in the current conversation and tell Elliott about
the gap instead of placing it in shared GBrain or Craft.

Capture is disabled. Never try to write through this skill. A future ingestion
workflow must apply [selected-fact-policy.md](references/selected-fact-policy.md),
preserve provenance, and receive separate write approval.
