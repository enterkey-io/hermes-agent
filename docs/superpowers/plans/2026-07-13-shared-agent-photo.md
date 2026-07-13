# Shared Hermes Agent Photo Implementation Plan

> Execute with isolated changes, focused tests, and no paid image calls during verification.

**Goal:** Replace profile-forked Hermes photo skills with one tested shared package while preserving profile-specific identity and prompting knowledge.

**Architecture:** Commit the canonical package in Hermes, deploy it through the existing shared-skills directory, and archive profile-local shadow copies. Resolve all runtime data from `$HERMES_HOME` and require explicit CLI approval for provider calls.

**Tech Stack:** Python 3, requests, Pillow, pytest, Hermes external skill discovery.

---

### Task 1: Add behavioral tests

**Files:**
- Create: `tests/skills/test_agent_photo_shared.py`

1. Test profile root and curated seed selection.
2. Test that preview requires no paid approval.
3. Test that generation refuses without `--approved`.
4. Test one-provider default and opt-in fallback.
5. Test profile media mirroring and one `MEDIA:` line per result.

### Task 2: Build the shared package

**Files:**
- Create: `skills/media/agent-photo/SKILL.md`
- Create: `skills/media/agent-photo/requirements.txt`
- Create: `skills/media/agent-photo/scripts/generate.py`
- Create: `skills/media/agent-photo/scripts/identity_parser.py`
- Create: `skills/media/agent-photo/scripts/prompt_profiles.py`
- Create: `skills/media/agent-photo/references/photo-prompting-rules.md`

1. Port the strongest existing Alina/Xenia logic.
2. Replace NanoClaw paths with profile resolution.
3. Remove direct reads of NanoClaw credential files.
4. Add approval and fallback gates.
5. Keep provider calls mockable and delivery Hermes-native.

### Task 3: Verify and commit

1. Run the new test module and skill size/frontmatter checks.
2. Run existing external-skill discovery tests.
3. Run prompt preview against Alina, Grace, and Xenia profiles.
4. Commit the package on `feature/agent-photo`.

### Task 4: Deploy and remove shadows

1. Deploy the committed package to `/home/elliott/.hermes/shared-skills/agent-photo`.
2. Move agent-specific references to each profile's `photo-guidance/` directory.
3. Archive old local skill packages outside active `skills/` trees.
4. Verify each profile discovers exactly one `agent-photo` skill.
5. Restart only affected Hermes gateways if fresh discovery requires it.

### Task 5: Record the migration pattern

1. Add the shared-versus-profile-local rule to the NanoClaw-to-Hermes migration runbook.
2. Update Grace's migration ledger and skill-gap inventory.
3. Record hashes and rollback locations for archived copies.
