---
name: agent-photo
description: Create or edit an identity-locked Hermes agent photo.
version: 2.1.0
author: Elliott Hermes
license: MIT
platforms: [linux]
prerequisites:
  commands: [hermes-agent-photo]
metadata:
  hermes:
    tags: [media, image-generation, identity, hermes]
---

# Agent Photo

## Overview

Generate a photo of the active Hermes agent using that profile's identity file and curated seed image. This package is shared by every Hermes profile; identity, baselines, private guidance, and output stay local to the active profile. The trusted wrapper can read the profile's exact, owner-managed Characters binding and privately cache that agent's references without exposing the app-wide credential.

The script supports Gemini, Grok, and Seedream. Each call can spend money. Its standalone CLI refuses generation unless the caller passes `--approved`; the native tool supplies that flag only after verifying authority.

## Native Hermes Execution

When the `agent_photo` tool is available, use it rather than terminal commands.
Call `action="instructions"` for its current contract, `action="characters_status"`
for bound reference metadata, and `action="preview"` with the proposed `prompt`
for a no-spend preview. The CLI examples below are for operator-only standalone
use; they do not require the conversational agent to have terminal or key access.

Use `action="references"` to list local image paths under this profile's
`assets/`, `baselines/`, and `media/`. Both catalogs accept `offset` and `limit`
(one to five entries) and return `next_offset`; fetch another page when needed.
For preview or generation, pass `source_images` with those profile-relative paths
and/or `characters_photo_ids` from the bound Characters catalog. Select at most
four extra images in total. The input order is the identity seed, selected
Characters photos, then local sources, preserving order within each list.
When several references depict the same person, say so explicitly in the prompt
and describe each reference's role. These arguments do not grant general file
access, cross-profile references, extra generations, or additional retries.
The instructions action includes the shared prompting rules in full; it does
not require a separate file read.

A direct photo request in Elliott's current message authorizes one native
`action="generate"` call without another approval question. The native tool
defaults to one Gemini attempt and, if that fails, one Grok attempt in the same
call. Set `fallback_to_grok=false` for an explicit Gemini-only request. An
explicit `model="grok"` or `model="seedream"` makes one attempt without fallback.
Do not invoke generation again to retry a failed or uncertain call, and do not
use the standalone `--allow-fallback` flag. The trusted wrapper supplies the
configured credentials; never request, inspect or change keys to invoke this tool.
Deliver each returned `MEDIA:` line exactly once through the native reply.
This native contract takes precedence over standalone CLI instructions below.

## When To Use

Use this skill when the user asks the agent to:

- take, send, or create a new photo of herself;
- show herself in a described scene, outfit, pose, or mood;
- create variations of an agent photo;
- edit an existing agent photo while preserving identity.

Do not use it for unrelated illustrations, diagrams, screenshots, web images, or images of other people. Do not generate a photo merely because a schedule fired, a prior request exists, or a photo might improve a check-in.

## Profile Contract

The active profile is `$HERMES_HOME`. The script reads and writes only the current profile by default:

```text
$HERMES_HOME/
  identity.md                 required
  assets/lifelike-seed.png   preferred local seed; jpg/jpeg also supported
  assets/characters/         private Characters cache; wrapper-managed
  baselines/                  optional body or pose references
  photo-guidance/             optional agent-specific learned guidance
  media/photos/               durable generated-photo history
  media/                      channel-visible attachment copies
```

Never borrow another agent's seed, baseline, identity, or private guidance. A local `assets/lifelike-seed.*` is the explicit override. If it is absent and the profile has an exact Characters binding, the wrapper retrieves only the bound `lifelike-seed` into `assets/characters/`. It never matches by display name. Missing, ambiguous, or mismatched bindings fail closed.

Use Characters as a curated reference library, not a bulk image dump:

- Run `"$HOME/.local/bin/hermes-agent-photo" --characters-status` to see safe metadata for this agent's bound library without spending money.
- Add `--characters-photo PHOTO_ID` to a Gemini, Grok, or Seedream request to use one specifically selected bound photo. The wrapper verifies ownership and caches it privately before passing the local copy to the generator.
- Prefer identity roles such as `lifelike-seed`, `lifelike-smile`, and `face-closeup-*` for facial consistency. Use `body-shape-*` and `baseline-*` only for the body or pose they document. Treat `style` and generic `upload` photos as inspiration, not identity truth.
- Select the smallest useful set, normally one seed plus one or two references. Do not send the whole Characters library to a provider.

Before a complex or identity-sensitive photo, inspect `$HERMES_HOME/photo-guidance/` if it exists. Use only guidance relevant to the current agent. General prompting behavior lives in `references/photo-prompting-rules.md` next to this file.

## Provider Choice

| Provider | Flag | Best fit | Important limit |
|---|---|---|---|
| Gemini | `--model gemini` | Default for high-quality everyday portraits and multi-person identity composition | Multiple ordered input images and one output |
| Grok | `--model grok` | Multiple variations or extra image references | Up to five input images; `-n` only when requested |
| Seedream | `--model seedream` | Multiple references or material Gemini rejects | One output in the current integration |

For standalone operator CLI use only, the script does not switch providers after failure unless the user separately authorizes paid retries and the caller passes `--allow-fallback`. Never add that flag as routine error handling. Native Hermes generation instead owns the bounded Gemini-to-Grok chain described above.

## Workflow

For native Hermes conversations, the execution path is `agent_photo`. For
standalone operator use, the execution path is `$HOME/.local/bin/hermes-agent-photo`.
Both preserve the trusted wrapper boundary. Never invoke the underlying generator
or a package runner directly.

### 1. Confirm authority

The current message must explicitly ask for a new or edited photo. A general permission, old request, check-in schedule, or implied desire is not enough. Completion criterion: you can point to the current request that authorizes one paid generation call.

### 2. Compose the scene

Describe the resulting frame, not the capture mechanism. Include one clear pose, expression, clothing state, setting, lighting, framing, and viewer angle. Let the seed carry face, hair, skin, jewelry, and other stable identity details unless the user specifically asks to change something.

Read `references/photo-prompting-rules.md` when the scene is unusual, close-up, uses an extreme angle, or needs multiple reference images.

### 3. Preview without spending

```bash
"$HOME/.local/bin/hermes-agent-photo" \
  --preview-prompt \
  "close portrait, looking into the lens, relaxed half-smile, leather jacket, warm window light"
```

Review the final prompt for contradictory poses, invented identity traits, repeated jewelry, visible camera hardware, and an incorrect provider. Prompt preview never contacts an image provider.

### 4. Generate once

```bash
"$HOME/.local/bin/hermes-agent-photo" \
  --approved \
  --model gemini \
  "close portrait, looking into the lens, relaxed half-smile, leather jacket, warm window light"
```

For an existing profile-local baseline or source image:

```bash
"$HOME/.local/bin/hermes-agent-photo" \
  --approved \
  --model grok \
  --source "$HERMES_HOME/baselines/full-body.jpg" \
  "full-body portrait, standing naturally, evening interior light"
```

Use `-n 2` through `-n 10` with Grok only when the user explicitly requests options or variations. Completion criterion: the command returns success and every output path exists under the active profile.

### 5. Deliver exactly once

The script prints one line per generated image:

```text
MEDIA: $HERMES_HOME/media/<filename>.png
```

Include each printed `MEDIA:` line once in the assistant reply. Put the natural-language caption in that same reply. Do not also invoke another file, message, or attachment tool for those paths, and do not send a second copy of the caption.

## Options

```text
--preview-prompt         build the prompt without a provider call
--save-prompt PATH       save the built prompt inside the active profile
--approved               confirm a current explicit user request
--allow-fallback         authorize calls to alternate paid providers after failure
--model PROVIDER         gemini, grok, or seedream
--source PATH            add a regular png/jpg/jpeg/webp reference; repeat as needed
--characters-status      list this profile's bound Characters photo metadata; no provider call
--characters-photo ID    cache one exact bound Characters photo and use it as a source; repeat as needed
--output PATH            custom output inside the active profile only
-n COUNT                 Grok variations, 1 through 10
--aspect-ratio RATIO     Grok framing
--size WIDTHxHEIGHT      Gemini and Seedream dimensions
```

## Failure Handling

- Missing identity or seed: allow the wrapper's exact Characters binding fallback; if no valid bound seed exists, report the missing profile-relative item and do not substitute another profile's assets.
- Credential failure: report which provider is unavailable without printing credential values.
- Provider rejection or timeout: stop after the selected provider. Ask before a paid retry or provider switch.
- Bad output: do not silently reroll. Show the result or describe the defect and ask before another paid call.
- Attachment failure: verify the file exists in `$HERMES_HOME/media/`, then retry delivery of the existing file without generating again.

## Verification Checklist

- [ ] The current user message explicitly requested the photo.
- [ ] `$HERMES_HOME` resolves to this agent's profile.
- [ ] `identity.md` and the local or exact-bound Characters seed belong to this agent.
- [ ] The scene describes one coherent visible frame.
- [ ] Only the requested number of paid images was generated.
- [ ] Every generated file exists in `media/photos/` and `media/`.
- [ ] Each `MEDIA:` line appears once and no second attachment method was used.
