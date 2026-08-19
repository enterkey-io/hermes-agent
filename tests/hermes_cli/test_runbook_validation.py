from __future__ import annotations

import pytest

from hermes_cli.runbook_schema import RunbookValidationError, split_frontmatter


def valid_markdown(slug: str = "morning-message") -> str:
    return f"""---
id: rb-{slug}
slug: {slug}
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
inputs: {{}}
outputs: {{}}
permitted_writes: []
approval_rules: {{}}
retry: {{}}
timeout: {{}}
deduplication: {{}}
related: {{}}
---
# Morning Message
"""


def test_validates_required_frontmatter() -> None:
    parsed = split_frontmatter(valid_markdown())

    assert parsed.metadata["slug"] == "morning-message"
    assert parsed.body.startswith("# Morning")


def test_rejects_missing_required_field() -> None:
    markdown = valid_markdown().replace("owner_profile: grace\n", "")

    with pytest.raises(RunbookValidationError, match="owner_profile"):
        split_frontmatter(markdown)


def test_rejects_duplicate_steps() -> None:
    markdown = valid_markdown().replace(
        "  - step_key: collect\n    name: Collect inputs\n",
        "  - step_key: collect\n    name: Collect inputs\n"
        "  - step_key: collect\n    name: Collect again\n",
    )

    with pytest.raises(RunbookValidationError, match="duplicate step_key"):
        split_frontmatter(markdown)


def test_rejects_invalid_runtime_kind() -> None:
    markdown = valid_markdown().replace("kind: script", "kind: paperclip")

    with pytest.raises(RunbookValidationError, match="runtime kind"):
        split_frontmatter(markdown)


def test_rejects_nonoperational_owner_when_org_installed(monkeypatch) -> None:
    from pathlib import Path

    org_path = Path(__file__).parents[2] / "workforce" / "organization.yaml"
    monkeypatch.setenv("HERMES_WORKFORCE_ORG", str(org_path))
    markdown = valid_markdown().replace("owner_profile: grace", "owner_profile: amy")
    markdown = markdown.replace("related: {}", "related:\n  workforce_managed: true")
    with pytest.raises(RunbookValidationError, match="cannot own or execute"):
        split_frontmatter(markdown)


def test_generic_runbook_is_not_constrained_by_workforce_roster(monkeypatch) -> None:
    from pathlib import Path

    org_path = Path(__file__).parents[2] / "workforce" / "organization.yaml"
    monkeypatch.setenv("HERMES_WORKFORCE_ORG", str(org_path))
    markdown = valid_markdown().replace("owner_profile: grace", "owner_profile: amy")
    assert split_frontmatter(markdown).metadata["owner_profile"] == "amy"
