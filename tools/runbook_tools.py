"""Agent tools for the canonical Hermes runbook store."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from hermes_cli import runbook_store
from hermes_cli import workflow_registry as registry_db
from hermes_cli.runbook_schema import render_frontmatter, split_frontmatter
from tools.registry import registry, tool_error, tool_result


def _actor() -> str:
    home = Path(os.environ.get("HERMES_HOME", "")).expanduser()
    if home.parent.name == "profiles" and home.name:
        return home.name
    return "agent"


def _list(args: dict[str, Any]) -> str:
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


def _get(args: dict[str, Any]) -> str:
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


def _validate(args: dict[str, Any]) -> str:
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


def _propose(args: dict[str, Any]) -> str:
    slug = str(args.get("slug") or "").strip()
    markdown = str(args.get("markdown") or "")
    if not slug:
        return tool_error("slug is required")
    if not runbook_store.runbook_path(slug).exists():
        return tool_error(f"runbook not found: {slug}")
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
    return tool_result(success=True, slug=slug, proposal_path=str(path))


def _runs(args: dict[str, Any]) -> str:
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
    name="runbook_propose_edit",
    toolset="runbook",
    schema=_schema(
        "runbook_propose_edit",
        "Store a proposed runbook edit for human review without activating it.",
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
