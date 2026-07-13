# Tooling

Use only `/home/elliott/.hermes/hermes-agent/skills/productivity/craft-work/scripts/craft-ops-client.mjs`.
It sends one strict newline-delimited JSON request and receives one
response over `/run/user/1000/craft-ops/craft-ops.sock`, with a 4 MiB request/response
limit and 15-minute default timeout.

The request schema is:

```json
{"version":"1","requestId":"UUID","action":"contracts|run|resume|reconcile|rollback","input":{},"dryRun":false}
```

`contract` is required only for `run`. `runId` is required only for `resume`,
`reconcile`, and `rollback`. Unknown fields, invalid UUIDs, conflicting fields,
oversized input, symlink/non-socket paths, and oversized output are rejected.

Use `contracts` before selecting an operation. Routine agent access permits
`craft-read`, `craft-write`, `meeting-ingest`, `daily-update`, `weekly-update`,
`library-capture`, and `library-review` only when the broker exposes them.
Migration and rollback are operator-only. Never print inputs or capability
details in diagnostics. Keep the original run ID for recovery. A nonzero result,
`uncertain`, or `drift` stops the workflow; do not fall back to a direct remote
MCP connection or raw HTTP request.
