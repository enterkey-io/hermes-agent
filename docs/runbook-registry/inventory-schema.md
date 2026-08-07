# Workflow Inventory Schema

`scripts/workflow_inventory.py` is the Phase 1 read-only scanner for the
Runbook Registry and Paperclip retirement work. It writes seven reports:

- `workflow-inventory.json`
- `workflow-inventory.md`
- `paperclip-active-dependencies.json`
- `schedule-collision-report.json`
- `paperclip-export-reconciliation.json`
- `notification-path-inventory.json`
- `active-automation-authority-map.md`

The scanner records metadata, hashes, counts, and short redacted evidence
snippets. It must not copy credential values into any report.

## `workflow-inventory.json`

Top-level fields:

- `schema_version`: integer schema version.
- `generated_at`: UTC ISO timestamp.
- `hermes_root`: shared Hermes root that was scanned.
- `scan_roots`: roots scanned for active automation references.
- `profiles`: discovered Hermes profiles.
- `evidence`: redacted keyword evidence from scanned files.
- `host_schedules`: read-only host scheduling metadata.
- `paperclip`: discovered Paperclip install/config locations and local SQLite
  table counts where applicable.
- `paperclip_active_dependencies`: active Paperclip references that require
  migration or retirement review.
- `schedule_collision_report`: duplicate enabled Hermes Cron schedule buckets.
- `notification_path_inventory`: cron and file references to notification
  channels.
- `active_automation_authority_map`: normalized schedule/dependency ownership
  rows.
- `paperclip_export_reconciliation`: source-count placeholder used by the
  later archive/export step.
- `counts`: summary counts.

## Profile Records

Each `profiles[]` entry contains:

- `name`
- `path`
- `cron_jobs`
- `scripts`
- `plugins`
- `skills`
- `runbook_candidates`
- `env_keys`
- `has_agents_md`
- `has_config_yaml`

Cron job records intentionally omit prompt text. They include `prompt_sha256`
instead, plus redacted schedule/delivery metadata:

- `id`
- `name`
- `enabled`
- `schedule`
- `schedule_raw`
- `deliver`
- `skills`
- `workflow_id`
- `prompt_sha256`
- `classification`
- `source_path`

## Classifications

Evidence classifications are:

- `active-runtime`
- `active-documentation`
- `historical-archive`
- `migration-evidence`
- `generated-output`
- `credential-reference`
- `unknown-review-required`

Archive and conversation-history paths remain in `evidence`, but they are
excluded from active Paperclip dependency counts.

## Paperclip Reconciliation

`paperclip-export-reconciliation.json` is metadata-only in this phase. It lists
discovered Paperclip locations and read-only local SQLite table counts. Full
source/export count reconciliation belongs to the archive/export phase after
the exporter exists.

## Usage

Default production run:

```bash
python scripts/workflow_inventory.py --output-dir /home/elliott/.hermes/inventory
```

Isolated test-style run:

```bash
python scripts/workflow_inventory.py \
  --hermes-root /tmp/hermes \
  --scan-root /tmp/hermes \
  --paperclip-root /tmp/paperclip \
  --skip-host-commands \
  --output-dir /tmp/reports
```
