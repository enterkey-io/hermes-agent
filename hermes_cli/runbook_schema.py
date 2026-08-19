"""Validation and parsing for canonical Hermes runbook Markdown files."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml

from hermes_cli.workflow_models import RUNTIME_KINDS, WORKFLOW_STATUSES


REQUIRED_FRONTMATTER_FIELDS = {
    "id",
    "slug",
    "title",
    "purpose",
    "owner_profile",
    "status",
    "runtime",
    "schedules",
    "steps",
    "inputs",
    "outputs",
    "permitted_writes",
    "approval_rules",
    "retry",
    "timeout",
    "deduplication",
    "related",
}


class RunbookValidationError(ValueError):
    """Raised when a runbook Markdown document is invalid."""


@dataclass(frozen=True)
class ParsedRunbook:
    metadata: dict[str, Any]
    body: str


def split_frontmatter(markdown: str) -> ParsedRunbook:
    """Split a Markdown document into YAML frontmatter and body."""
    if not markdown.startswith("---\n"):
        raise RunbookValidationError("runbook must start with YAML frontmatter")
    end = markdown.find("\n---\n", 4)
    if end == -1:
        raise RunbookValidationError("runbook frontmatter is not closed")
    raw = markdown[4:end]
    body = markdown[end + 5 :]
    try:
        metadata = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise RunbookValidationError(f"invalid YAML frontmatter: {exc}") from exc
    if not isinstance(metadata, dict):
        raise RunbookValidationError("runbook frontmatter must be a mapping")
    validate_frontmatter(metadata)
    return ParsedRunbook(metadata=metadata, body=body)


def render_frontmatter(metadata: dict[str, Any], body: str) -> str:
    """Render metadata and body back into canonical Markdown."""
    validate_frontmatter(metadata)
    frontmatter = yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True)
    return f"---\n{frontmatter}---\n{body.lstrip()}"


def validate_frontmatter(metadata: dict[str, Any]) -> None:
    missing = sorted(REQUIRED_FRONTMATTER_FIELDS - set(metadata))
    if missing:
        raise RunbookValidationError(f"missing required frontmatter fields: {missing}")
    _require_string(metadata, "id")
    _require_string(metadata, "slug")
    _require_string(metadata, "title")
    _require_string(metadata, "purpose")
    _require_string(metadata, "owner_profile")
    status = _require_string(metadata, "status")
    if status not in WORKFLOW_STATUSES:
        raise RunbookValidationError(f"invalid status: {status!r}")
    runtime = metadata["runtime"]
    if not isinstance(runtime, dict):
        raise RunbookValidationError("runtime must be a mapping")
    kind = runtime.get("kind")
    if kind not in RUNTIME_KINDS:
        raise RunbookValidationError(f"invalid runtime kind: {kind!r}")
    if not isinstance(metadata["schedules"], list):
        raise RunbookValidationError("schedules must be a list")
    steps = metadata["steps"]
    if not isinstance(steps, list) or not steps:
        raise RunbookValidationError("steps must be a non-empty list")
    seen_keys: set[str] = set()
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise RunbookValidationError(f"step {index} must be a mapping")
        step_key = step.get("step_key")
        if not isinstance(step_key, str) or not step_key.strip():
            raise RunbookValidationError(f"step {index} missing step_key")
        if step_key in seen_keys:
            raise RunbookValidationError(f"duplicate step_key: {step_key}")
        seen_keys.add(step_key)
        if not isinstance(step.get("name"), str) or not step["name"].strip():
            raise RunbookValidationError(f"step {step_key} missing name")
    for key in (
        "inputs",
        "outputs",
        "permitted_writes",
        "approval_rules",
        "retry",
        "timeout",
        "deduplication",
        "related",
    ):
        if not isinstance(metadata[key], (dict, list)):
            raise RunbookValidationError(f"{key} must be a mapping or list")
    _validate_workforce_profiles_if_configured(metadata)


def _validate_workforce_profiles_if_configured(metadata: dict[str, Any]) -> None:
    from hermes_cli.workforce_org import (
        WorkforceOrganizationError,
        is_workforce_managed,
        load_organization,
        organization_path,
        validate_workflow_profiles,
    )

    if not is_workforce_managed(metadata):
        return
    path = organization_path()
    if not path.is_file():
        return
    executors = [
        step.get("executor_profile")
        for step in metadata.get("steps", [])
        if isinstance(step, dict)
    ]
    try:
        validate_workflow_profiles(
            load_organization(path), str(metadata.get("owner_profile") or ""), executors
        )
    except WorkforceOrganizationError as exc:
        raise RunbookValidationError(str(exc)) from exc


def _require_string(metadata: dict[str, Any], key: str) -> str:
    value = metadata.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RunbookValidationError(f"{key} must be a non-empty string")
    return value
