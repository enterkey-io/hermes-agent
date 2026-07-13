# Workflow Contracts

## Meeting Or Transcript

1. Resolve the immutable meeting/transcript source ID and search Craft.
2. Create or update the meeting record with source link, date, attendees,
   people, organization/project relations, summary, decisions, and action
   candidates.
3. Preserve decision owners and project/task relations; do not flatten them
   into unattributed prose.
4. Deduplicate native Craft tasks. Route software or agent execution to
   Paperclip instead of duplicating its lifecycle.
5. Read back the complete record. Select approved reusable knowledge for
   GBrain `shared_craft` separately, with Craft ID/revision provenance and
   explicit exclusion of private relationship material. Use `shared_meetings`
   only for approved meeting evidence that has not been derived from the
   canonical Craft record; never write the same fact to both sources.

## Bookmarks And Research

Canonical URL is identity across collectors. Keep all source IDs and ingestion
timestamps on one canonical record, beginning in the reviewed state required by
the verified contract. Never use a random filename as identity.

## Legacy Tasks

Treat Todoist and Microsoft To Do as read-only evidence. Match an existing
Craft task first, then search Paperclip for development work. Preserve one
Craft task, link Paperclip when applicable, and route unresolved conflicts to
task migration triage rather than recreating blindly.

## Development Handoff

Read the current Craft specification and revision. Search Paperclip before
creating work; preserve the Craft ID/link/revision, resolve project, goal,
parent, assignee, dependencies, acceptance criteria, and a separate QA/review
path. Paperclip owns execution state; Craft receives verified outcome links.
