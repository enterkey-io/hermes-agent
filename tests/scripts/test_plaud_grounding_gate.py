from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[2] / "scripts" / "plaud_grounding_gate.py"
SPEC = importlib.util.spec_from_file_location("plaud_grounding_gate", SCRIPT)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gate
SPEC.loader.exec_module(gate)

RECORDING = "recording-123456"


def transcript() -> dict:
    return {
        "schema": "plaud-transcript-v1",
        "recording_id": RECORDING,
        "kind": "official_transcript",
        "segments": [
            {"speaker": "Carrie", "content": "The finance transition stays in QBO while the long-term workflow is reviewed.", "start_time": 0, "end_time": 9000},
            {"speaker": "Elliott", "content": "We need the final sales posting. I will send the final posting to Heath tomorrow.", "start_time": 9000, "end_time": 19000},
            {"speaker": "Elliott", "content": "I will not send the candidate brief because the role was canceled.", "start_time": 19000, "end_time": 26000},
            {"speaker": "Elliott", "content": "I can send a draft later if the team wants one.", "start_time": 26000, "end_time": 33000},
            {"speaker": "Carrie", "content": "We are maintaining the current referral model for this quarter.", "start_time": 33000, "end_time": 41000},
            {"speaker": "Carrie", "content": "We have not decided to replace QBO.", "start_time": 41000, "end_time": 48000},
            {"speaker": "Elliott", "content": "I will never send the canceled candidate brief.", "start_time": 48000, "end_time": 55000},
        ],
    }


def source_metadata(payload: bytes, *, fallback: bool = False) -> dict:
    return {
        "schema": "plaud-transcript-v1",
        "recording_id": RECORDING,
        "kind": "exact_empty" if fallback else "official_transcript",
        "transcript_output": {
            "verified": True,
            "kind": "fallback_transcript" if fallback else "official_transcript",
            "sha256": gate._sha(payload),
            "bytes": len(payload),
        },
    }


def draft(*, with_action: bool = False) -> dict:
    return {
        "version": 2,
        "recording_id": RECORDING,
        "classification": {
            "type": "internal",
            "confidence": 0.91,
            "alternatives": [{"type": "unknown", "confidence": 0.09}],
            "review_flag": False,
            "basis_segment": 1,
        },
        "purpose_segment": 1,
        "highlights": [3, 4, 7],
        "chapters": [
            {"first_segment": 1, "last_segment": 2},
            {"first_segment": 3, "last_segment": 7},
        ],
        "decisions": [5],
        "actions": [{"segment": 2, "state": "next", "explicit_elliott_owned": True}] if with_action else [],
        "open_questions": [6],
        "risks": [],
        "uncertainties": [],
        "transcript_quality": [],
    }


def private_source_files(tmp_path: Path, *, fallback: bool = False) -> tuple[Path, Path]:
    source_path = tmp_path / "raw-transcript"
    metadata_path = tmp_path / "source-metadata.json"
    if fallback:
        source_bytes = ("\n\n".join(row["content"] for row in transcript()["segments"]) + "\n").encode()
    else:
        source_bytes = json.dumps(transcript()).encode()
    source_path.write_bytes(source_bytes)
    metadata_path.write_text(json.dumps(source_metadata(source_bytes, fallback=fallback)))
    source_path.chmod(0o600)
    metadata_path.chmod(0o600)
    return source_path, metadata_path


def private_source_index(tmp_path: Path) -> Path:
    index_path = tmp_path / "grounding-source.txt"
    index_path.write_bytes(gate.render_source_index(gate._segments(transcript(), RECORDING)))
    index_path.chmod(0o600)
    return index_path


def private_selected_envelope(tmp_path: Path) -> Path:
    path = tmp_path / "selected-envelope.json"
    path.write_text(json.dumps({
        "inventory_complete": True,
        "recording": {
            "duration_ms": 55000,
            "id": RECORDING,
            "recorded_at": "2026-09-10T19:00:32Z",
            "title": "Internal finance and integration check-in",
        },
        "status": "ready",
        "wakeAgent": True,
    }))
    path.chmod(0o600)
    return path


def test_valid_zero_action_draft_renders_only_complete_source_segments() -> None:
    validated = gate.validate(transcript(), draft(), RECORDING)
    summary = gate.render_summary(validated)
    actions = json.loads(gate.render_grounded_actions(validated))
    assert b"The finance transition stays in QBO while the long-term workflow is reviewed." in summary
    assert b"No explicit Elliott-owned commitment" in summary
    assert actions == {"version": 2, "recording_id": RECORDING, "actions": []}


def test_freeform_incident_claim_cannot_enter_selector_schema() -> None:
    bad = draft()
    bad["highlights"][0] = {
        "text": "A billboard campaign and candidate conversation have deadlines this week.",
        "evidence": [{"segment": 1, "quote": "finance transition stays in QBO"}],
    }
    with pytest.raises(gate.GroundingError, match="must be an integer"):
        gate.validate(transcript(), bad, RECORDING)


def test_exact_incident_action_shape_cannot_enter_selector_schema() -> None:
    bad = draft()
    bad["actions"] = [{
        "name": "Set up candidate conversation with Carrie and referral contact",
        "state": "next",
        "explicit_elliott_owned": True,
        "claim": {
            "text": "Elliott will set up a candidate conversation with a referral contact.",
            "evidence": [{"segment": 1, "quote": "The finance transition stays in QBO"}],
        },
    }]
    with pytest.raises(gate.GroundingError, match="keys differ"):
        gate.validate(transcript(), bad, RECORDING)


def test_whole_segment_render_preserves_negation() -> None:
    candidate = draft()
    summary = gate.render_summary(gate.validate(transcript(), candidate, RECORDING))
    assert b"I will not send the candidate brief because the role was canceled." in summary
    assert b"I will send the candidate brief" not in summary


@pytest.mark.parametrize("segment", [3, 4, 7])
def test_negation_and_capability_offer_are_not_commitments(segment: int) -> None:
    candidate = draft()
    candidate["actions"] = [{"segment": segment, "state": "next", "explicit_elliott_owned": True}]
    with pytest.raises(gate.GroundingError, match="unconditional"):
        gate.validate(transcript(), candidate, RECORDING)


def test_undecided_statement_is_not_an_explicit_decision() -> None:
    candidate = draft()
    candidate["decisions"] = [6]
    candidate["open_questions"] = []
    with pytest.raises(gate.GroundingError, match="non-negated decision"):
        gate.validate(transcript(), candidate, RECORDING)


def test_status_remaining_in_question_is_not_a_decision() -> None:
    source = transcript()
    source["segments"][5]["content"] = "The replacement decision remains in question."
    candidate = draft()
    candidate["decisions"] = [6]
    candidate["open_questions"] = []
    with pytest.raises(gate.GroundingError, match="non-negated decision"):
        gate.validate(source, candidate, RECORDING)


def test_render_then_finalize_actions_binds_exact_source_receipt_and_note(tmp_path: Path) -> None:
    source_path, metadata_path = private_source_files(tmp_path)
    source_index_path = private_source_index(tmp_path)
    selected_envelope_path = private_selected_envelope(tmp_path)
    draft_path = tmp_path / "grounding-draft.json"
    draft_path.write_text(json.dumps(draft(with_action=True)))
    draft_path.chmod(0o600)
    render_args = type("Args", (), {
        "recording_id": RECORDING,
        "transcript_file": source_path,
        "source_metadata_file": metadata_path,
        "source_index_file": source_index_path,
        "draft_file": draft_path,
        "summary_output": tmp_path / "summary.md",
        "actions_output": tmp_path / "grounded-actions.json",
        "receipt_output": tmp_path / "grounding-receipt.json",
    })()
    receipt = gate.render(render_args)
    assert receipt["status"] == "validated"
    assert receipt["rendering"] == "whole-source-segments"
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in (render_args.summary_output, render_args.actions_output, render_args.receipt_output))

    finalize_args = type("Args", (), {
        "recording_id": RECORDING,
        "transcript_file": source_path,
        "source_metadata_file": metadata_path,
        "source_index_file": source_index_path,
        "draft_file": draft_path,
        "receipt_file": render_args.receipt_output,
        "selected_envelope_file": selected_envelope_path,
        "note_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "plan_output": tmp_path / "action-plan.json",
        "delivery_output": tmp_path / "delivery.txt",
    })()
    result = gate.finalize_actions(finalize_args)
    plan = json.loads(finalize_args.plan_output.read_text())
    assert result["action_count"] == 1
    assert plan["actions"][0]["name"].startswith("Plaud follow-up: We need the final sales posting")
    assert "I will send the final posting to Heath tomorrow" in plan["actions"][0]["note"]
    assert "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" in plan["actions"][0]["note"]
    assert result["delivery_sha256"] == gate._sha(finalize_args.delivery_output.read_bytes())
    assert finalize_args.delivery_output.read_text().startswith("Internal finance and integration check-in\n")


def test_receipt_drift_blocks_action_finalization(tmp_path: Path) -> None:
    source_path, metadata_path = private_source_files(tmp_path)
    source_index_path = private_source_index(tmp_path)
    selected_envelope_path = private_selected_envelope(tmp_path)
    draft_path = tmp_path / "grounding-draft.json"
    draft_path.write_text(json.dumps(draft(with_action=True)))
    draft_path.chmod(0o600)
    render_args = type("Args", (), {
        "recording_id": RECORDING,
        "transcript_file": source_path,
        "source_metadata_file": metadata_path,
        "source_index_file": source_index_path,
        "draft_file": draft_path,
        "summary_output": tmp_path / "summary.md",
        "actions_output": tmp_path / "grounded-actions.json",
        "receipt_output": tmp_path / "grounding-receipt.json",
    })()
    gate.render(render_args)
    changed = json.loads(draft_path.read_text())
    changed["classification"]["confidence"] = 0.90
    draft_path.write_text(json.dumps(changed))
    draft_path.chmod(0o600)
    finalize_args = type("Args", (), {
        "recording_id": RECORDING,
        "transcript_file": source_path,
        "source_metadata_file": metadata_path,
        "source_index_file": source_index_path,
        "draft_file": draft_path,
        "receipt_file": render_args.receipt_output,
        "selected_envelope_file": selected_envelope_path,
        "note_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "plan_output": tmp_path / "action-plan.json",
        "delivery_output": tmp_path / "delivery.txt",
    })()
    with pytest.raises(gate.GroundingError, match="does not bind"):
        gate.finalize_actions(finalize_args)
    assert not finalize_args.plan_output.exists()


def test_selected_envelope_mismatch_blocks_both_final_outputs(tmp_path: Path) -> None:
    source_path, metadata_path = private_source_files(tmp_path)
    source_index_path = private_source_index(tmp_path)
    selected_envelope_path = private_selected_envelope(tmp_path)
    draft_path = tmp_path / "grounding-draft.json"
    draft_path.write_text(json.dumps(draft()))
    draft_path.chmod(0o600)
    render_args = type("Args", (), {
        "recording_id": RECORDING,
        "transcript_file": source_path,
        "source_metadata_file": metadata_path,
        "source_index_file": source_index_path,
        "draft_file": draft_path,
        "summary_output": tmp_path / "summary.md",
        "actions_output": tmp_path / "grounded-actions.json",
        "receipt_output": tmp_path / "grounding-receipt.json",
    })()
    gate.render(render_args)
    changed = json.loads(selected_envelope_path.read_text())
    changed["recording"]["id"] = "different-recording"
    selected_envelope_path.write_text(json.dumps(changed))
    selected_envelope_path.chmod(0o600)
    finalize_args = type("Args", (), {
        "recording_id": RECORDING,
        "transcript_file": source_path,
        "source_metadata_file": metadata_path,
        "source_index_file": source_index_path,
        "draft_file": draft_path,
        "receipt_file": render_args.receipt_output,
        "selected_envelope_file": selected_envelope_path,
        "note_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "plan_output": tmp_path / "action-plan.json",
        "delivery_output": tmp_path / "delivery.txt",
    })()
    with pytest.raises(gate.GroundingError, match="recording_id does not match"):
        gate.finalize_actions(finalize_args)
    assert not finalize_args.plan_output.exists()
    assert not finalize_args.delivery_output.exists()


def test_selected_envelope_requires_timezone_aware_recorded_at(tmp_path: Path) -> None:
    path = private_selected_envelope(tmp_path)
    value = json.loads(path.read_text())
    value["recording"]["recorded_at"] = "2026-09-10T19:00:32"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    with pytest.raises(gate.GroundingError, match="timezone-aware"):
        gate._selected_title(value, RECORDING)


def test_index_source_is_private_complete_and_refuses_overwrite(tmp_path: Path) -> None:
    source_path, metadata_path = private_source_files(tmp_path)
    args = type("Args", (), {
        "recording_id": RECORDING,
        "transcript_file": source_path,
        "source_metadata_file": metadata_path,
        "source_output": tmp_path / "grounding-source.txt",
    })()
    receipt = gate.index_source(args)
    output = args.source_output.read_text()
    assert receipt["segment_count"] == 7
    assert args.source_output.stat().st_mode & 0o777 == 0o600
    assert "[segment 3 | Elliott | 0:19-0:26]" in output
    assert transcript()["segments"][2]["content"] in output
    with pytest.raises(gate.GroundingError, match="refusing to overwrite"):
        gate.index_source(args)


def test_render_rejects_a_source_index_from_other_bytes(tmp_path: Path) -> None:
    source_path, metadata_path = private_source_files(tmp_path)
    draft_path = tmp_path / "grounding-draft.json"
    index_path = tmp_path / "grounding-source.txt"
    draft_path.write_text(json.dumps(draft()))
    index_path.write_text("unbound index\n")
    draft_path.chmod(0o600)
    index_path.chmod(0o600)
    args = type("Args", (), {
        "recording_id": RECORDING,
        "transcript_file": source_path,
        "source_metadata_file": metadata_path,
        "source_index_file": index_path,
        "draft_file": draft_path,
        "summary_output": tmp_path / "summary.md",
        "actions_output": tmp_path / "grounded-actions.json",
        "receipt_output": tmp_path / "grounding-receipt.json",
    })()
    with pytest.raises(gate.GroundingError, match="source index does not bind"):
        gate.render(args)
    assert not args.summary_output.exists()


def test_cli_index_then_render_exercises_private_runtime_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "processing"
    work = root / "run-test"
    work.mkdir(parents=True, mode=0o700)
    work.chmod(0o700)
    source_path, metadata_path = private_source_files(work)
    draft_path = work / "grounding-draft.json"
    draft_path.write_text(json.dumps(draft()))
    draft_path.chmod(0o600)
    monkeypatch.setattr(gate, "WORK_ROOT", root)
    monkeypatch.setattr(sys, "argv", [
        str(SCRIPT),
        "index-source",
        "--recording-id", RECORDING,
        "--transcript-file", str(source_path),
        "--source-metadata-file", str(metadata_path),
        "--source-output", str(work / "grounding-source.txt"),
    ])
    assert gate.main() == 0
    monkeypatch.setattr(sys, "argv", [
        str(SCRIPT),
        "render",
        "--recording-id", RECORDING,
        "--transcript-file", str(source_path),
        "--source-metadata-file", str(metadata_path),
        "--source-index-file", str(work / "grounding-source.txt"),
        "--draft-file", str(draft_path),
        "--summary-output", str(work / "summary.md"),
        "--actions-output", str(work / "grounded-actions.json"),
        "--receipt-output", str(work / "grounding-receipt.json"),
    ])
    assert gate.main() == 0
    assert b"whole-source-segments" in (work / "grounding-receipt.json").read_bytes()
    assert (work / "summary.md").stat().st_mode & 0o777 == 0o600


def test_schema_rejects_unreviewed_extra_fields() -> None:
    bad = copy.deepcopy(draft())
    bad["freeform_model_notes"] = "not part of the provider contract"
    with pytest.raises(gate.GroundingError, match="extra"):
        gate.validate(transcript(), bad, RECORDING)


def test_segment_cannot_be_repeated_across_semantic_sections() -> None:
    bad = draft()
    bad["uncertainties"] = [6]
    with pytest.raises(gate.GroundingError, match="only one summary section"):
        gate.validate(transcript(), bad, RECORDING)


def test_total_source_budget_includes_basis_purpose_and_actions() -> None:
    source = transcript()
    source["segments"][0]["content"] += " " + ("basis " * 520).rstrip()
    source["segments"][1]["content"] += " " + ("followup " * 330).rstrip()
    with pytest.raises(gate.GroundingError, match="all rendered source"):
        gate.validate(source, draft(with_action=True), RECORDING)


def test_verified_fallback_can_ground_summary_but_never_owner_action(tmp_path: Path) -> None:
    source_path, metadata_path = private_source_files(tmp_path, fallback=True)
    source_bytes = source_path.read_bytes()
    metadata = json.loads(metadata_path.read_text())
    validated = gate.validate_source(source_bytes, metadata, draft(), RECORDING)
    assert all(not segment.timed and segment.speaker == "Unknown" for segment in validated.segments)
    assert b"unattributed source block" in gate.render_summary(validated)
    with pytest.raises(gate.GroundingError, match="no Elliott-spoken evidence"):
        gate.validate_source(source_bytes, metadata, draft(with_action=True), RECORDING)


def test_gate_has_no_provider_or_message_surface() -> None:
    source = SCRIPT.read_text()
    for forbidden in ("official_session", "replace_fragment", "create_tasks", "update_tasks", "send_message", "cron.jobs"):
        assert forbidden not in source
