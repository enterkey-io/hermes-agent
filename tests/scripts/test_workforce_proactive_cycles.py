from pathlib import Path

import pytest

from hermes_cli.runbook_schema import split_frontmatter
from scripts.workforce_proactive_cycles import render, render_room_tokens


ROOT = Path(__file__).parents[2]
TEMPLATE = ROOT / "workforce/runbooks/workforce-proactive-operating-cycles/RUNBOOK.md"


def _room_map(tmp_path: Path, *, omit: str | None = None) -> Path:
    rooms = {
        "admin", "executive-support", "director-product",
        "director-agent-systems", "director-operations", "director-marketing",
        "director-trading", "director-finance", "director-vision",
    }
    path = tmp_path / "rooms.yaml"
    lines = [
        f"{room}: {index:08d}-1111-1111-1111-111111111111"
        for index, room in enumerate(sorted(rooms - {omit} if omit else rooms), 1)
    ]
    path.write_text("\n".join(lines) + "\n")
    return path


def test_rendered_proactive_cycles_are_complete_and_workforce_managed(tmp_path):
    parsed = split_frontmatter(render(TEMPLATE, _room_map(tmp_path)))
    assert parsed.metadata["related"]["workforce_managed"] is True
    schedules = parsed.metadata["schedules"]
    assert parsed.metadata["runtime"]["max_iterations"] == 8
    assert parsed.metadata["runtime"]["tool_budget"]["max_calls"] == 6
    assert "kanban_create" not in parsed.metadata["runtime"]["tool_budget"]["allowed_tools"]
    assert "kanban_archive_stale" in parsed.metadata["runtime"]["tool_budget"]["allowed_tools"]
    assert "workforce_handoff" in parsed.metadata["runtime"]["tool_budget"]["allowed_tools"]
    assert len(schedules) == 11
    assert {item["profile"] for item in schedules} == {
        "chloe", "milena", "emily", "alina", "main", "bridgette",
        "xenia", "maggie", "mel", "aurora",
    }
    assert all(item["deliver"] == "local" for item in schedules)
    assert all(
        item["enabled_toolsets"] == ["kanban", "workforce", "runbook", "no_mcp"]
        for item in schedules
    )
    assert "at most six tool calls total" in parsed.body
    assert "Never enumerate the whole board or workforce" in parsed.body
    assert "always deliver locally" in parsed.body


def test_render_does_not_require_rooms_for_local_internal_cycles(tmp_path):
    parsed = split_frontmatter(render(TEMPLATE, _room_map(tmp_path, omit="director-trading")))
    assert all(schedule["deliver"] == "local" for schedule in parsed.metadata["schedules"])


def test_render_rejects_a_malformed_room_uuid(tmp_path):
    room_map = _room_map(tmp_path)
    room_map.write_text(
        room_map.read_text().replace(
            "00000001-1111-1111-1111-111111111111",
            "------------------------------------",
        )
    )
    template = ROOT / "workforce/runbooks/aurora-weekly-workforce-goal-alignment/RUNBOOK.md"
    with pytest.raises(ValueError, match="invalid room UUID"):
        render_room_tokens(template, room_map)


def test_render_validates_schedule_identity_instead_of_a_fixed_count(tmp_path):
    reduced = tmp_path / "RUNBOOK.md"
    text = TEMPLATE.read_text()
    start = text.index("- id: milena-executive-follow-through")
    end = text.index("- id: product-outcome-review")
    text = text[:start] + text[end:]
    start = text.index("- step_key: milena_reconcile")
    end = text.index("- step_key: director_product")
    reduced.write_text(text[:start] + text[end:])
    parsed = split_frontmatter(render(reduced, _room_map(tmp_path)))
    assert len(parsed.metadata["schedules"]) == 10


def test_render_rejects_an_unbounded_proactive_toolset(tmp_path):
    unsafe = tmp_path / "RUNBOOK.md"
    unsafe.write_text(
        TEMPLATE.read_text().replace(
            "enabled_toolsets: [kanban, workforce, runbook, no_mcp]",
            "enabled_toolsets: [hermes-cli]",
            1,
        )
    )
    with pytest.raises(ValueError, match="bounded proactive toolsets"):
        render(unsafe, _room_map(tmp_path))


def test_render_rejects_user_visible_delivery_for_internal_cycle(tmp_path):
    unsafe = tmp_path / "RUNBOOK.md"
    unsafe.write_text(TEMPLATE.read_text().replace("deliver: local", "deliver: buzz:00000001-1111-1111-1111-111111111111", 1))
    with pytest.raises(ValueError, match="local-only"):
        render(unsafe, _room_map(tmp_path))


def test_room_only_render_supports_other_managed_runbooks(tmp_path):
    template = ROOT / "workforce/runbooks/aurora-weekly-workforce-goal-alignment/RUNBOOK.md"
    rendered = render_room_tokens(template, _room_map(tmp_path))
    parsed = split_frontmatter(rendered)
    assert parsed.metadata["slug"] == "aurora-weekly-workforce-goal-alignment"
    assert parsed.metadata["schedules"][0]["deliver"].startswith("buzz:")
    assert "<ROOM_UUID:" not in rendered
