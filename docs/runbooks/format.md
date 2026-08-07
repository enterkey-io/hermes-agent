# Hermes Runbook Format

Canonical runbooks are Markdown files named `RUNBOOK.md` under the shared
machine-level root:

```text
~/.hermes/runbooks/<slug>/RUNBOOK.md
```

The YAML frontmatter is the structured workflow definition. The Markdown body
is the human-readable procedure.

Required frontmatter fields:

- `id`
- `slug`
- `title`
- `purpose`
- `owner_profile`
- `status`
- `runtime`
- `schedules`
- `steps`
- `inputs`
- `outputs`
- `permitted_writes`
- `approval_rules`
- `retry`
- `timeout`
- `deduplication`
- `related`

Example:

```markdown
---
id: rb-morning-message
slug: morning-message
title: Morning Message
purpose: Prepare and deliver the morning message.
owner_profile: grace
status: active
runtime:
  kind: script
  ref: grace/scripts/morning.py
schedules: []
steps:
  - step_key: collect
    name: Collect inputs
inputs: {}
outputs: {}
permitted_writes: []
approval_rules: {}
retry: {}
timeout: {}
deduplication: {}
related: {}
---
# Morning Message
```

Dashboard or human-approved saves write atomically and create revision
snapshots. Agent proposals are stored under `.proposals/` and do not replace
the active runbook.
