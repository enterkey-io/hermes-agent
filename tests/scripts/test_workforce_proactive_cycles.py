from pathlib import Path

import pytest

from hermes_cli.runbook_schema import split_frontmatter
from scripts.workforce_proactive_cycles import render


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
    assert len(schedules) == 10
    assert {item["profile"] for item in schedules} == {
        "chloe", "milena", "emily", "alina", "main", "bridgette",
        "xenia", "maggie", "mel", "aurora",
    }
    assert all("<ROOM_UUID:" not in item["deliver"] for item in schedules)


def test_render_fails_closed_when_a_room_mapping_is_missing(tmp_path):
    with pytest.raises(ValueError, match="director-trading"):
        render(TEMPLATE, _room_map(tmp_path, omit="director-trading"))


def test_render_rejects_a_malformed_room_uuid(tmp_path):
    room_map = _room_map(tmp_path)
    room_map.write_text(
        room_map.read_text().replace(
            "00000001-1111-1111-1111-111111111111",
            "------------------------------------",
        )
    )
    with pytest.raises(ValueError, match="invalid room UUID"):
        render(TEMPLATE, room_map)


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
    assert len(parsed.metadata["schedules"]) == 9
