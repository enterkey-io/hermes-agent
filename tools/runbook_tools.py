"""Agent tools for the canonical Hermes runbook store."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any

from hermes_cli import runbook_store
from hermes_cli import workflow_registry as registry_db
from hermes_cli.runbook_schema import render_frontmatter, split_frontmatter
from hermes_constants import get_default_hermes_root
from tools.registry import registry, tool_error, tool_result


def _actor() -> str:
    home = Path(os.environ.get("HERMES_HOME", "")).expanduser()
    if home.parent.name == "profiles" and home.name:
        return home.name
    return "agent"


def _list(args: dict[str, Any], **_kwargs: Any) -> str:
    query = str(args.get("query") or "").strip()
    owner = str(args.get("owner_profile") or "").strip().lower()
    status = str(args.get("status") or "").strip().lower()
    records = runbook_store.search_runbooks(query) if query else runbook_store.list_runbooks()
    items = [record.to_dict() for record in records]
    if owner:
        items = [item for item in items if item["owner_profile"].lower() == owner]
    if status:
        items = [item for item in items if item["status"].lower() == status]
    return tool_result({"count": len(items), "runbooks": items})


def _get(args: dict[str, Any], **_kwargs: Any) -> str:
    slug = str(args.get("slug") or "").strip()
    if not slug:
        return tool_error("slug is required")
    path = runbook_store.runbook_path(slug)
    if not path.exists():
        return tool_error(f"runbook not found: {slug}")
    parsed = runbook_store.read_runbook(path)
    record = next(
        item for item in runbook_store.list_runbooks() if item.slug == parsed.metadata["slug"]
    )
    return tool_result(
        {
            "runbook": record.to_dict(),
            "metadata": parsed.metadata,
            "body": parsed.body,
        }
    )


def _validate(args: dict[str, Any], **_kwargs: Any) -> str:
    markdown = str(args.get("markdown") or "")
    try:
        parsed = split_frontmatter(markdown)
    except Exception as exc:
        return tool_error(str(exc), valid=False)
    return tool_result(
        valid=True,
        slug=parsed.metadata["slug"],
        normalized_markdown=render_frontmatter(parsed.metadata, parsed.body),
    )


def _store_proposal(
    args: dict[str, Any],
    *,
    require_new: bool = False,
) -> str:
    slug = str(args.get("slug") or "").strip()
    markdown = str(args.get("markdown") or "")
    if not slug:
        return tool_error("slug is required")
    target_exists = runbook_store.runbook_path(slug).exists()
    if require_new and target_exists:
        return tool_error(f"runbook already exists: {slug}; propose an edit instead")
    try:
        parsed = split_frontmatter(markdown)
        if parsed.metadata["slug"] != slug:
            return tool_error("runbook slug does not match proposal target")
        path = runbook_store.propose_edit(
            slug,
            markdown,
            proposed_by=_actor(),
            summary=str(args.get("summary") or "").strip() or None,
        )
    except Exception as exc:
        return tool_error(str(exc))
    return tool_result(
        success=True,
        slug=slug,
        proposal_kind="edit" if target_exists else "create",
        proposal_path=str(path),
    )


def _propose(args: dict[str, Any], **_kwargs: Any) -> str:
    # Keep old callers working when the first proposal creates the runbook.
    return _store_proposal(args)


def _propose_create(args: dict[str, Any], **_kwargs: Any) -> str:
    return _store_proposal(args, require_new=True)


def _runs(args: dict[str, Any], **_kwargs: Any) -> str:
    slug = str(args.get("slug") or "").strip()
    limit = max(1, min(int(args.get("limit") or 20), 100))
    if not slug:
        return tool_error("slug is required")
    try:
        with registry_db.connect_closing() as conn:
            definition = registry_db.get_definition_by_slug(conn, slug)
            rows = conn.execute(
                "SELECT * FROM workflow_runs WHERE workflow_id = ? "
                "ORDER BY started_at DESC LIMIT ?",
                (definition.id, limit),
            ).fetchall()
            runs = []
            for row in rows:
                item = dict(row)
                steps = conn.execute(
                    "SELECT * FROM workflow_step_runs WHERE workflow_run_id = ? "
                    "ORDER BY step_key, attempt",
                    (item["id"],),
                ).fetchall()
                item["steps"] = [dict(step) for step in steps]
                runs.append(item)
    except Exception as exc:
        return tool_error(str(exc))
    return tool_result({"slug": slug, "count": len(runs), "runs": runs})


def _legacy_connect() -> sqlite3.Connection:
    path = (
        get_default_hermes_root()
        / "archives"
        / "paperclip"
        / "current"
        / "legacy-work.db"
    )
    if not path.is_file():
        raise FileNotFoundError("Paperclip Legacy Work archive is not available")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _legacy_match(value: str) -> str:
    tokens = re.findall(r"[A-Za-z0-9_-]+", value)
    return " ".join(f'"{token}"*' for token in tokens[:12])


def _legacy_search(args: dict[str, Any], **_kwargs: Any) -> str:
    query = str(args.get("query") or "").strip()
    entity_type = str(args.get("entity_type") or "").strip()
    limit = max(1, min(int(args.get("limit") or 20), 100))
    if not query:
        return tool_error("query is required")
    match = _legacy_match(query)
    if not match:
        return tool_result({"count": 0, "results": []})
    conditions = ["legacy_search MATCH ?"]
    params: list[Any] = [match]
    if entity_type:
        conditions.append("e.entity_type = ?")
        params.append(entity_type)
    try:
        with _legacy_connect() as conn:
            rows = conn.execute(
                "SELECT e.entity_type, e.entity_id, e.legacy_identifier, e.title, "
                "e.status, e.owner, e.updated_at FROM legacy_entities e "
                "JOIN legacy_search ON legacy_search.entity_type=e.entity_type "
                "AND legacy_search.entity_id=e.entity_id WHERE "
                + " AND ".join(conditions)
                + " ORDER BY e.updated_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
    except (FileNotFoundError, sqlite3.Error) as exc:
        return tool_error(str(exc))
    return tool_result({"count": len(rows), "results": [dict(row) for row in rows]})


def _legacy_get(args: dict[str, Any], **_kwargs: Any) -> str:
    entity_type = str(args.get("entity_type") or "").strip()
    entity_id = str(args.get("entity_id") or "").strip()
    if not entity_type or not entity_id:
        return tool_error("entity_type and entity_id are required")
    try:
        with _legacy_connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM legacy_entities WHERE entity_type=? AND entity_id=?",
                (entity_type, entity_id),
            ).fetchone()
    except (FileNotFoundError, sqlite3.Error) as exc:
        return tool_error(str(exc))
    if row is None:
        return tool_error("Legacy Work item not found")
    return tool_result({"entity_type": entity_type, "entity": json.loads(row["payload_json"])})


def _always() -> bool:
    return True


def _schema(name: str, description: str, properties: dict[str, Any], required=None):
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required or [],
        },
    }


registry.register(
    name="runbook_list",
    toolset="runbook",
    schema=_schema(
        "runbook_list",
        "List canonical Hermes runbooks, optionally filtered by owner or status.",
        {
            "owner_profile": {"type": "string"},
            "status": {"type": "string", "enum": ["draft", "active", "paused", "degraded", "retired"]},
        },
    ),
    handler=_list,
    check_fn=_always,
)
registry.register(
    name="runbook_search",
    toolset="runbook",
    schema=_schema(
        "runbook_search",
        "Search canonical Hermes runbook titles, slugs, purposes, owners, and status.",
        {"query": {"type": "string"}},
        ["query"],
    ),
    handler=_list,
    check_fn=_always,
)
registry.register(
    name="runbook_get",
    toolset="runbook",
    schema=_schema(
        "runbook_get",
        "Read the current canonical Markdown procedure and metadata for a runbook slug.",
        {"slug": {"type": "string"}},
        ["slug"],
    ),
    handler=_get,
    check_fn=_always,
    max_result_size_chars=100000,
)
registry.register(
    name="runbook_validate",
    toolset="runbook",
    schema=_schema(
        "runbook_validate",
        "Validate candidate canonical runbook Markdown without saving it.",
        {"markdown": {"type": "string"}},
        ["markdown"],
    ),
    handler=_validate,
    check_fn=_always,
    max_result_size_chars=100000,
)
registry.register(
    name="runbook_propose_create",
    toolset="runbook",
    schema=_schema(
        "runbook_propose_create",
        "Store a proposed new canonical runbook for human review without activating it.",
        {
            "slug": {"type": "string"},
            "markdown": {"type": "string"},
            "summary": {"type": "string"},
        },
        ["slug", "markdown"],
    ),
    handler=_propose_create,
    check_fn=_always,
    max_result_size_chars=100000,
)
registry.register(
    name="runbook_propose_edit",
    toolset="runbook",
    schema=_schema(
        "runbook_propose_edit",
        "Store a proposed runbook edit for human review without activating it. For compatibility, this also accepts the first proposal for a missing runbook.",
        {
            "slug": {"type": "string"},
            "markdown": {"type": "string"},
            "summary": {"type": "string"},
        },
        ["slug", "markdown"],
    ),
    handler=_propose,
    check_fn=_always,
    max_result_size_chars=100000,
)
registry.register(
    name="runbook_runs",
    toolset="runbook",
    schema=_schema(
        "runbook_runs",
        "Inspect recent execution and step history for a canonical runbook.",
        {"slug": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
        ["slug"],
    ),
    handler=_runs,
    check_fn=_always,
)
registry.register(
    name="legacy_work_search",
    toolset="runbook",
    schema=_schema(
        "legacy_work_search",
        "Search the sanitized read-only Paperclip Legacy Work archive for historical projects, tasks, comments, routines, and runs.",
        {
            "query": {"type": "string"},
            "entity_type": {
                "type": "string",
                "enum": ["project", "goal", "issue", "comment", "routine", "routine_run"],
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        ["query"],
    ),
    handler=_legacy_search,
    check_fn=_always,
)
registry.register(
    name="legacy_work_get",
    toolset="runbook",
    schema=_schema(
        "legacy_work_get",
        "Read one sanitized historical item from the read-only Paperclip Legacy Work archive.",
        {
            "entity_type": {
                "type": "string",
                "enum": ["project", "goal", "issue", "comment", "routine", "routine_run"],
            },
            "entity_id": {"type": "string"},
        },
        ["entity_type", "entity_id"],
    ),
    handler=_legacy_get,
    check_fn=_always,
    max_result_size_chars=100000,
)
