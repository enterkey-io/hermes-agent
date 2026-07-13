# Shared Hermes Agent Photo Design

## Goal

Provide one maintained `agent-photo` skill to every Hermes profile while keeping each agent's identity, reference images, learned private guidance, and generated media isolated in that profile.

## Boundaries

- The shared package owns provider integrations, prompt construction, profile-local path handling, delivery instructions, tests, and general prompting guidance.
- `$HERMES_HOME` owns `identity.md`, `assets/lifelike-seed.*`, optional `baselines/`, optional `photo-guidance/`, and `media/`.
- A current user request to generate or edit a photo is required before a paid provider call. The CLI enforces this with `--approved`.
- The selected provider is called once. Cross-provider fallback is disabled unless the user separately authorizes retries and the caller supplies `--allow-fallback`.
- Generated files are copied into the active profile's `media/` directory and returned as exactly one `MEDIA:` line per image. The agent must not use another attachment tool for the same file.

## Shared And Profile-Specific Guidance

General lessons from the existing Alina and Xenia skills move into shared references. Guidance tied to one agent's seed, body references, or tested personal appearance remains under that profile's `photo-guidance/` directory. The skill tells an agent to inspect that directory when present, but shared code never contains an agent name or profile path.

## Runtime Layout

The canonical package is committed at `skills/media/agent-photo/` on `feature/agent-photo`. Production profiles discover a deployed copy at `/home/elliott/.hermes/shared-skills/agent-photo` through their existing `skills.external_dirs` configuration. The deployment helper refuses a partial migration when active profile-local shadows remain unless `--archive-local` is supplied, then moves those copies under the excluded `.archive` tree and verifies that none remain active.

## Security And Cost Controls

The implementation reads provider credentials only from the process environment or `op read`; it does not parse NanoClaw secret files. Tests and smoke checks use prompt preview and mocked provider calls only. Output and errors never print credential values. Source images must resolve inside the active profile unless an explicit source path is supplied and validated as a regular image file.

## Verification

Tests cover profile resolution, seed precedence, paid-call approval, preview behavior, fallback behavior, media mirroring, and output line count. Deployment verification checks that Alina, Grace, and Xenia each discover exactly one shared skill and can build a prompt using their own identity and seed without an API call.
