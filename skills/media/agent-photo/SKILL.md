---
name: agent-photo
description: Create or edit an identity-locked Hermes agent photo.
version: 2.0.0
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

Generate a photo of the active Hermes agent using that profile's identity file and curated seed image. This package is shared by every Hermes profile; identity, baselines, private guidance, and output stay local to the active profile.

The script supports Gemini, Grok, and Seedream. Each call can spend money. It refuses generation unless you pass `--approved`, which means the user explicitly requested the photo in the current conversation.

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
  assets/lifelike-seed.png   required; jpg/jpeg also supported
  baselines/                  optional body or pose references
  photo-guidance/             optional agent-specific learned guidance
  media/photos/               durable generated-photo history
  media/                      channel-visible attachment copies
```

Never borrow another agent's seed, baseline, identity, or private guidance. If required profile data is missing, stop and report the missing path.

Before a complex or identity-sensitive photo, inspect `$HERMES_HOME/photo-guidance/` if it exists. Use only guidance relevant to the current agent. General prompting behavior lives in `references/photo-prompting-rules.md` next to this file.

## Provider Choice

| Provider | Flag | Best fit | Important limit |
|---|---|---|---|
| Gemini | `--model gemini` | Default for high-quality everyday portraits | One seed image and one output |
| Grok | `--model grok` | Multiple variations or extra image references | Up to five input images; `-n` only when requested |
| Seedream | `--model seedream` | Multiple references or material Gemini rejects | One output in the current integration |

Use one provider per request. The script does not switch providers after failure unless the user separately authorizes paid retries and you pass `--allow-fallback`. Never add that flag as routine error handling.

## Workflow

The sole execution path for previews and paid generation is
`$HOME/.local/bin/hermes-agent-photo`. Never invoke the underlying
generator or a package runner directly.

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
--output PATH            custom output inside the active profile only
-n COUNT                 Grok variations, 1 through 10
--aspect-ratio RATIO     Grok framing
--size WIDTHxHEIGHT      Gemini and Seedream dimensions
```

## Failure Handling

- Missing identity or seed: report the exact missing profile-relative item; do not substitute another profile's assets.
- Credential failure: report which provider is unavailable without printing credential values.
- Provider rejection or timeout: stop after the selected provider. Ask before a paid retry or provider switch.
- Bad output: do not silently reroll. Show the result or describe the defect and ask before another paid call.
- Attachment failure: verify the file exists in `$HERMES_HOME/media/`, then retry delivery of the existing file without generating again.

## Verification Checklist

- [ ] The current user message explicitly requested the photo.
- [ ] `$HERMES_HOME` resolves to this agent's profile.
- [ ] `identity.md` and `assets/lifelike-seed.*` belong to this agent.
- [ ] The scene describes one coherent visible frame.
- [ ] Only the requested number of paid images was generated.
- [ ] Every generated file exists in `media/photos/` and `media/`.
- [ ] Each `MEDIA:` line appears once and no second attachment method was used.
