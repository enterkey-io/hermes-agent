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
            {"speaker": "Elliott", "content": "There are two referral models, and I think the client-referral model is more likely to work.", "start_time": 19000, "end_time": 30000},
            {"speaker": "Carrie", "content": "The next integration check-in is already on the calendar.", "start_time": 30000, "end_time": 37000},
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


def evidence(segment: int, quote: str) -> list[dict]:
    return [{"segment": segment, "quote": quote}]


def claim(text: str, segment: int, quote: str) -> dict:
    return {"text": text, "evidence": evidence(segment, quote)}


def draft(*, with_action: bool = False) -> dict:
    action_rows = []
    if with_action:
        action_rows = [{
            "name": "Send final sales posting to Heath",
            "state": "next",
            "explicit_elliott_owned": True,
            "claim": claim(
                "Elliott will send the final sales posting to Heath tomorrow.",
                2,
                "I will send the final posting to Heath tomorrow.",
            ),
        }]
    return {
        "version": 1,
        "recording_id": RECORDING,
        "classification": {
            "type": "internal",
            "confidence": 0.91,
            "alternatives": [{"type": "unknown", "confidence": 0.09}],
            "review_flag": False,
            "basis": claim("An internal finance and integration workflow review.", 1, "finance transition stays in QBO while the long-term workflow is reviewed"),
        },
        "purpose": claim("Review the finance transition and integration workflow.", 1, "finance transition stays in QBO while the long-term workflow is reviewed"),
        "highlights": [
            claim("The finance transition stays in QBO during review.", 1, "finance transition stays in QBO while the long-term workflow is reviewed"),
            claim("Elliott will send the final sales posting to Heath tomorrow.", 2, "I will send the final posting to Heath tomorrow"),
            claim("Two referral models were discussed for client referrals.", 3, "There are two referral models"),
        ],
        "chapters": [
            {"title": "Finance transition and QBO workflow", "first_segment": 1, "last_segment": 1},
            {"title": "Sales posting and referral models", "first_segment": 2, "last_segment": 3},
            {"title": "Next integration check-in", "first_segment": 4, "last_segment": 4},
        ],
        "decisions": [claim("The finance transition stays in QBO during review.", 1, "finance transition stays in QBO while the long-term workflow is reviewed")],
        "actions": action_rows,
        "open_questions": [claim("The long-term finance workflow remains under review.", 1, "long-term workflow is reviewed")],
        "risks": [],
        "uncertainties": [claim("The preferred long-term finance workflow is not yet stated.", 1, "long-term workflow is reviewed")],
        "transcript_quality": [claim("The final posting follow-up is stated clearly.", 2, "final posting to Heath tomorrow")],
    }


def test_valid_zero_action_draft_renders_deterministic_private_outputs(tmp_path: Path) -> None:
    validated = gate.validate(transcript(), draft(), RECORDING)
    summary = gate.render_summary(validated)
    actions = json.loads(gate.render_grounded_actions(validated))
    assert b"No explicit Elliott-owned commitment" in summary
    assert b"candidate conversation" not in summary
    assert actions == {"version": 1, "recording_id": RECORDING, "actions": []}


def test_exact_incident_shape_rejects_unrelated_candidate_action() -> None:
    bad = draft()
    bad["actions"] = [{
        "name": "Set up candidate conversation with referral contact",
        "state": "next",
        "explicit_elliott_owned": True,
        "claim": claim(
            "Elliott will set up a candidate conversation with a referral contact.",
            3,
            "There are two referral models, and I think the client-referral model is more likely to work.",
        ),
    }]
    with pytest.raises(gate.GroundingError, match="lexically grounded|first-person Elliott commitment"):
        gate.validate(transcript(), bad, RECORDING)


def test_unrelated_summary_claim_is_rejected_even_with_real_excerpt() -> None:
    bad = draft()
    bad["highlights"][0] = claim(
        "A billboard campaign and logo rollout have deadlines this week.",
        1,
        "finance transition stays in QBO while the long-term workflow is reviewed",
    )
    with pytest.raises(gate.GroundingError, match="lexically grounded"):
        gate.validate(transcript(), bad, RECORDING)


def test_quote_must_be_exact_excerpt() -> None:
    bad = draft()
    bad["purpose"]["evidence"][0]["quote"] = "This sentence does not occur in the transcript."
    with pytest.raises(gate.GroundingError, match="not an exact excerpt"):
        gate.validate(transcript(), bad, RECORDING)


def test_conditional_owner_language_is_not_an_explicit_action() -> None:
    source = transcript()
    source["segments"][1]["content"] = "Maybe after approval I will send the final posting to Heath."
    candidate = draft(with_action=True)
    candidate["highlights"][1] = claim("Elliott may send the final posting to Heath after approval.", 2, "Maybe after approval I will send the final posting to Heath")
    candidate["transcript_quality"][0] = claim("The posting follow-up is conditional on approval.", 2, "Maybe after approval I will send the final posting to Heath")
    candidate["actions"][0]["claim"] = claim("Elliott will send the final posting to Heath after approval.", 2, "Maybe after approval I will send the final posting to Heath")
    with pytest.raises(gate.GroundingError, match="unconditional"):
        gate.validate(source, candidate, RECORDING)


def test_render_then_finalize_actions_binds_exact_source_receipt_and_note(tmp_path: Path) -> None:
    source_path = tmp_path / "raw-transcript"
    metadata_path = tmp_path / "source-metadata.json"
    draft_path = tmp_path / "grounding-draft.json"
    source_bytes = json.dumps(transcript()).encode()
    source_path.write_bytes(source_bytes)
    metadata_path.write_text(json.dumps(source_metadata(source_bytes)))
    draft_path.write_text(json.dumps(draft(with_action=True)))
    for path in (source_path, metadata_path, draft_path):
        path.chmod(0o600)
    render_args = type("Args", (), {
        "recording_id": RECORDING,
        "transcript_file": source_path,
        "source_metadata_file": metadata_path,
        "draft_file": draft_path,
        "summary_output": tmp_path / "summary.md",
        "actions_output": tmp_path / "grounded-actions.json",
        "receipt_output": tmp_path / "grounding-receipt.json",
    })()
    receipt = gate.render(render_args)
    assert receipt["status"] == "validated"
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in (render_args.summary_output, render_args.actions_output, render_args.receipt_output))

    finalize_args = type("Args", (), {
        "recording_id": RECORDING,
        "transcript_file": source_path,
        "source_metadata_file": metadata_path,
        "draft_file": draft_path,
        "receipt_file": render_args.receipt_output,
        "note_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "plan_output": tmp_path / "action-plan.json",
    })()
    result = gate.finalize_actions(finalize_args)
    plan = json.loads(finalize_args.plan_output.read_text())
    assert result["action_count"] == 1
    assert plan["actions"][0]["name"] == "Send final sales posting to Heath"
    assert "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" in plan["actions"][0]["note"]
    assert "recording-123456" in plan["actions"][0]["note"]


def test_receipt_drift_blocks_action_finalization(tmp_path: Path) -> None:
    source_path = tmp_path / "raw-transcript"
    metadata_path = tmp_path / "source-metadata.json"
    draft_path = tmp_path / "grounding-draft.json"
    source_bytes = json.dumps(transcript()).encode()
    source_path.write_bytes(source_bytes)
    metadata_path.write_text(json.dumps(source_metadata(source_bytes)))
    draft_path.write_text(json.dumps(draft(with_action=True)))
    for path in (source_path, metadata_path, draft_path):
        path.chmod(0o600)
    render_args = type("Args", (), {
        "recording_id": RECORDING,
        "transcript_file": source_path,
        "source_metadata_file": metadata_path,
        "draft_file": draft_path,
        "summary_output": tmp_path / "summary.md",
        "actions_output": tmp_path / "grounded-actions.json",
        "receipt_output": tmp_path / "grounding-receipt.json",
    })()
    gate.render(render_args)
    changed = json.loads(draft_path.read_text())
    changed["purpose"]["text"] = "Review the QBO finance transition and integration workflow."
    draft_path.write_text(json.dumps(changed))
    draft_path.chmod(0o600)
    finalize_args = type("Args", (), {
        "recording_id": RECORDING,
        "transcript_file": source_path,
        "source_metadata_file": metadata_path,
        "draft_file": draft_path,
        "receipt_file": render_args.receipt_output,
        "note_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "plan_output": tmp_path / "action-plan.json",
    })()
    with pytest.raises(gate.GroundingError, match="does not bind"):
        gate.finalize_actions(finalize_args)
    assert not finalize_args.plan_output.exists()


def test_schema_rejects_unreviewed_extra_fields() -> None:
    bad = copy.deepcopy(draft())
    bad["freeform_model_notes"] = "not part of the provider contract"
    with pytest.raises(gate.GroundingError, match="extra"):
        gate.validate(transcript(), bad, RECORDING)


def test_verified_fallback_text_can_ground_summary_but_never_owner_action() -> None:
    source_bytes = ("\n\n".join(row["content"] for row in transcript()["segments"]) + "\n").encode()
    validated = gate.validate_source(source_bytes, source_metadata(source_bytes, fallback=True), draft(), RECORDING)
    assert all(not segment.timed and segment.speaker == "Unknown" for segment in validated.segments)
    assert b"unattributed source block" in gate.render_summary(validated)
    with pytest.raises(gate.GroundingError, match="no Elliott-spoken evidence"):
        gate.validate_source(source_bytes, source_metadata(source_bytes, fallback=True), draft(with_action=True), RECORDING)


def test_gate_has_no_provider_or_message_surface() -> None:
    source = SCRIPT.read_text()
    for forbidden in ("official_session", "replace_fragment", "create_tasks", "update_tasks", "send_message", "cron.jobs"):
        assert forbidden not in source
