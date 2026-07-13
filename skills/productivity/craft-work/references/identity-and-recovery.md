# Identity And Recovery

## Identity Order

1. Existing stable Craft record ID.
2. Canonical URL for web research and bookmarks.
3. Source system plus immutable source ID.
4. A reviewed domain key defined by the Craft Collection schema.

Never identify a record from a filename, generated title, or fuzzy title match.
For the same canonical URL from several collectors, maintain one canonical
record and retain each source event/provenance.

## Mutation States

- `absent`: no identity/marker exists; creation may proceed.
- `verified`: expected record and fields match; return its stable ID.
- `conflict`: identity resolves to competing records or authorities; stop.
- `uncertain`: a mutation may have landed but cannot be proved; reconcile.
- `drift`: a record differs from the expected preimage/revision; stop for
  review before update or rollback.

Every mutation requires a durable run ID, deterministic idempotency marker,
operation log, independent readback, and update preimage. On interruption use
the same run. Never recover `uncertain` by issuing a fresh create.

## Escalation

Escalate unresolved `conflict`, `uncertain`, or `drift` to Elliott in the
current conversation channel. Preserve the blocker on the canonical Craft
record when a verified update is possible; use Paperclip instead when the
blocker belongs to development execution. Do not choose a new authority,
promote historical evidence, or roll back on Elliott's behalf.
