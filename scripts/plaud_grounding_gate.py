#!/usr/bin/env python3
"""Render source-extractive Plaud summaries before provider-facing workflow steps.

The model selects whole transcript segments and semantic sections in a strict
draft. This tool owns all rendered prose, validates conservative decision and
commitment signals, and has no provider, registry, or delivery surface.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


ALLOWED_TYPES = {
    "external_client",
    "internal",
    "coaching_1on1",
    "sales_discovery",
    "brainstorm_idea",
    "personal_voice_note",
    "unknown",
}
ALLOWED_STATES = {"inbox", "next", "waiting", "scheduled", "someday", "later"}
WORK_ROOT = Path("/home/elliott/.hermes/profiles/milena/cache/plaud-processing")
SELECTOR_SECTIONS = (
    "highlights",
    "decisions",
    "open_questions",
    "risks",
    "uncertainties",
    "transcript_quality",
)
TOP_LEVEL_KEYS = {
    "version",
    "recording_id",
    "classification",
    "purpose_segment",
    "highlights",
    "chapters",
    "decisions",
    "actions",
    "open_questions",
    "risks",
    "uncertainties",
    "transcript_quality",
}
COMMITMENT_RE = re.compile(
    r"\b(?:i\s+will|i['\u2019]ll|i\s+am\s+going\s+to|i['\u2019]m\s+going\s+to)\b",
    re.IGNORECASE,
)
NEGATED_COMMITMENT_RE = re.compile(
    r"\b(?:i\s+will\s+(?:not|never)|i\s+won['\u2019]t|"
    r"i\s+am\s+not\s+going\s+to|i['\u2019]m\s+not\s+going\s+to)\b",
    re.IGNORECASE,
)
CONDITIONAL_OR_WEAK_RE = re.compile(
    r"\b(?:if|maybe|might|perhaps|probably|possibly|could|would|try|hope|want)\b",
    re.IGNORECASE,
)
DECISION_RE = re.compile(
    r"\b(?:we\s+(?:decided|agreed)|the\s+decision\s+is|"
    r"we(?:['\u2019]re|\s+are)\s+(?:going\s+with|keeping|maintaining)|"
    r"we\s+will\s+(?:keep|use|maintain|move|proceed)|"
    r"(?:stays?|remains?)\s+(?:in|on|with|unchanged)|maintaining\s+as)\b",
    re.IGNORECASE,
)
UNDECIDED_RE = re.compile(
    r"\b(?:not|haven['\u2019]t|hasn['\u2019]t|hadn['\u2019]t)\b(?:\W+\w+){0,3}\W+"
    r"(?:decided|agreed|decision)|\b(?:no|without)\s+(?:final\s+)?decision\b",
    re.IGNORECASE,
)
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


class GroundingError(ValueError):
    """The draft cannot safely cross the provider-effects boundary."""


@dataclass(frozen=True)
class Segment:
    number: int
    speaker: str
    content: str
    start_ms: int
    end_ms: int
    timed: bool = True


@dataclass(frozen=True)
class ValidatedDraft:
    raw: Mapping[str, Any]
    segments: tuple[Segment, ...]
    claim_count: int
    evidence_count: int


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise GroundingError(f"{label} keys differ: missing={sorted(expected - actual)}, extra={sorted(actual - expected)}")


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise GroundingError(f"{label} must be an object")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise GroundingError(f"{label} must be an array")
    return value


def _text(value: Any, label: str, *, minimum: int = 1, maximum: int = 800) -> str:
    if not isinstance(value, str) or value != value.strip() or not (minimum <= len(value) <= maximum):
        raise GroundingError(f"{label} must be trimmed text of length {minimum}..{maximum}")
    if "\x00" in value or "\r" in value or "\n" in value:
        raise GroundingError(f"{label} must be one line")
    return value


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise GroundingError(f"{label} must be an integer >= {minimum}")
    return value


def _read_private(path: Path, label: str) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_uid != os.getuid()
            ):
                raise GroundingError(f"{label} must be an owner-only regular file")
            return handle.read()
    except OSError as exc:
        raise GroundingError(f"{label} is unavailable or unsafe") from exc


def _load_json(path: Path, label: str) -> tuple[Any, bytes]:
    payload = _read_private(path, label)
    try:
        return json.loads(payload), payload
    except json.JSONDecodeError as exc:
        raise GroundingError(f"{label} is unavailable or malformed") from exc


def _normalize(value: str) -> str:
    return " ".join(value.split())


def _timestamp(milliseconds: int) -> str:
    seconds = max(0, milliseconds // 1000)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def _segments(transcript: Any, recording_id: str) -> tuple[Segment, ...]:
    transcript = _mapping(transcript, "transcript")
    _require_exact_keys(transcript, {"schema", "recording_id", "kind", "segments"}, "transcript")
    if transcript.get("recording_id") != recording_id:
        raise GroundingError("transcript recording_id does not match")
    if transcript.get("kind") != "official_transcript" or not isinstance(transcript.get("schema"), str) or not transcript["schema"]:
        raise GroundingError("official transcript source contract is invalid")
    rows = _list(transcript.get("segments"), "transcript.segments")
    if not rows:
        raise GroundingError("transcript has no segments")
    result: list[Segment] = []
    previous_end = 0
    for offset, raw in enumerate(rows, 1):
        raw = _mapping(raw, f"segment {offset}")
        speaker = _text(raw.get("speaker"), f"segment {offset}.speaker", maximum=160)
        content = _text(raw.get("content"), f"segment {offset}.content", maximum=10000)
        start = _integer(raw.get("start_time"), f"segment {offset}.start_time")
        end = _integer(raw.get("end_time"), f"segment {offset}.end_time")
        if end < start or start < previous_end - 2000:
            raise GroundingError(f"segment {offset} has invalid timing")
        previous_end = max(previous_end, end)
        result.append(Segment(offset, speaker, content, start, end))
    return tuple(result)


def _source_metadata(metadata: Any, transcript_bytes: bytes, recording_id: str) -> tuple[str, str]:
    metadata = _mapping(metadata, "source metadata")
    _require_exact_keys(metadata, {"schema", "recording_id", "kind", "transcript_output"}, "source metadata")
    if metadata.get("recording_id") != recording_id or not isinstance(metadata.get("schema"), str) or not metadata["schema"]:
        raise GroundingError("source metadata identity is invalid")
    output = _mapping(metadata.get("transcript_output"), "source metadata.transcript_output")
    _require_exact_keys(output, {"verified", "kind", "sha256", "bytes"}, "source metadata.transcript_output")
    pair = metadata.get("kind"), output.get("kind")
    if pair not in {("official_transcript", "official_transcript"), ("exact_empty", "fallback_transcript")}:
        raise GroundingError("source metadata kind pair is invalid")
    if (
        output.get("verified") is not True
        or output.get("sha256") != _sha(transcript_bytes)
        or output.get("bytes") != len(transcript_bytes)
    ):
        raise GroundingError("source metadata does not bind the transcript bytes")
    return metadata["schema"], output["kind"]


def _fallback_segments(transcript_bytes: bytes) -> tuple[Segment, ...]:
    try:
        text = transcript_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GroundingError("fallback transcript is not UTF-8") from exc
    blocks = [_normalize(block) for block in re.split(r"\n\s*\n|(?<=\.)\s*\n", text) if block.strip()]
    if not blocks or any(len(block) > 10000 for block in blocks):
        raise GroundingError("fallback transcript has no bounded source blocks")
    return tuple(Segment(index, "Unknown", block, 0, 0, False) for index, block in enumerate(blocks, 1))


def _load_segments(transcript_bytes: bytes, metadata: Any, recording_id: str) -> tuple[Segment, ...]:
    schema, kind = _source_metadata(metadata, transcript_bytes, recording_id)
    if kind == "fallback_transcript":
        return _fallback_segments(transcript_bytes)
    try:
        transcript = json.loads(transcript_bytes)
    except json.JSONDecodeError as exc:
        raise GroundingError("official transcript is malformed") from exc
    segments = _segments(transcript, recording_id)
    if transcript["schema"] != schema:
        raise GroundingError("official transcript schema differs from source metadata")
    return segments


def _segment_number(value: Any, segments: Sequence[Segment], label: str) -> int:
    number = _integer(value, label, minimum=1)
    if number > len(segments):
        raise GroundingError(f"{label} references a missing segment")
    return number


def _selector_list(
    value: Any,
    segments: Sequence[Segment],
    label: str,
    *,
    minimum: int = 0,
    maximum: int = 12,
) -> list[int]:
    rows = _list(value, label)
    if not minimum <= len(rows) <= maximum:
        raise GroundingError(f"{label} must contain {minimum}..{maximum} segment numbers")
    result = [_segment_number(row, segments, f"{label}[{index}]") for index, row in enumerate(rows)]
    if len(result) != len(set(result)):
        raise GroundingError(f"{label} contains a duplicate segment")
    return result


def _validate_classification(raw: Any, segments: Sequence[Segment]) -> int:
    raw = _mapping(raw, "classification")
    _require_exact_keys(raw, {"type", "confidence", "alternatives", "review_flag", "basis_segment"}, "classification")
    kind = _text(raw.get("type"), "classification.type", maximum=40)
    confidence = raw.get("confidence")
    if kind not in ALLOWED_TYPES or isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise GroundingError("classification type or confidence is invalid")
    alternatives = _list(raw.get("alternatives"), "classification.alternatives")
    if len(alternatives) > 3:
        raise GroundingError("classification has too many alternatives")
    seen = {kind}
    confidence_total = float(confidence)
    for index, item in enumerate(alternatives):
        item = _mapping(item, f"classification.alternatives[{index}]")
        _require_exact_keys(item, {"type", "confidence"}, f"classification.alternatives[{index}]")
        alt_kind = _text(item.get("type"), f"classification.alternatives[{index}].type", maximum=40)
        alt_confidence = item.get("confidence")
        if alt_kind not in ALLOWED_TYPES or alt_kind in seen or isinstance(alt_confidence, bool) or not isinstance(alt_confidence, (int, float)) or not 0 <= alt_confidence <= 1:
            raise GroundingError("classification alternative is invalid")
        seen.add(alt_kind)
        confidence_total += float(alt_confidence)
    if confidence_total > 1.000001:
        raise GroundingError("classification confidence total exceeds 1")
    if not isinstance(raw.get("review_flag"), bool):
        raise GroundingError("classification.review_flag must be boolean")
    return _segment_number(raw.get("basis_segment"), segments, "classification.basis_segment")


def _validate_decision(segment: Segment, label: str) -> None:
    content = _normalize(segment.content)
    if not DECISION_RE.search(content) or UNDECIDED_RE.search(content):
        raise GroundingError(f"{label} lacks an explicit, non-negated decision signal")


def _validate_action(raw: Any, segments: Sequence[Segment], label: str) -> None:
    raw = _mapping(raw, label)
    _require_exact_keys(raw, {"segment", "state", "explicit_elliott_owned"}, label)
    state = _text(raw.get("state"), f"{label}.state", maximum=20)
    if state not in ALLOWED_STATES or raw.get("explicit_elliott_owned") is not True:
        raise GroundingError(f"{label} state or ownership marker is invalid")
    number = _segment_number(raw.get("segment"), segments, f"{label}.segment")
    segment = segments[number - 1]
    content = _normalize(segment.content)
    if segment.speaker.casefold() != "elliott":
        raise GroundingError(f"{label} has no Elliott-spoken evidence")
    if (
        not COMMITMENT_RE.search(content)
        or NEGATED_COMMITMENT_RE.search(content)
        or CONDITIONAL_OR_WEAK_RE.search(content)
        or "?" in content
    ):
        raise GroundingError(f"{label} has no unconditional first-person Elliott commitment")


def _validate_draft(draft: Any, recording_id: str, segments: tuple[Segment, ...]) -> ValidatedDraft:
    draft = _mapping(draft, "draft")
    _require_exact_keys(draft, TOP_LEVEL_KEYS, "draft")
    if draft.get("version") != 2 or draft.get("recording_id") != recording_id:
        raise GroundingError("draft version or recording_id is invalid")

    _validate_classification(draft.get("classification"), segments)
    _segment_number(draft.get("purpose_segment"), segments, "purpose_segment")
    claim_count = 2

    selected: dict[str, list[int]] = {}
    for section in SELECTOR_SECTIONS:
        selected[section] = _selector_list(
            draft.get(section),
            segments,
            section,
            minimum=3 if section == "highlights" else 0,
            maximum=5 if section == "highlights" else 12,
        )
        claim_count += len(selected[section])

    selected_numbers = [number for section in SELECTOR_SECTIONS for number in selected[section]]
    if len(selected_numbers) != len(set(selected_numbers)):
        raise GroundingError("a source segment may appear in only one summary section")
    selected_characters = sum(len(_normalize(segments[number - 1].content)) for number in selected_numbers)
    if selected_characters > 5000:
        raise GroundingError("selected summary source exceeds the 5000-character limit")

    for index, number in enumerate(selected["decisions"]):
        _validate_decision(segments[number - 1], f"decisions[{index}]")

    chapters = _list(draft.get("chapters"), "chapters")
    if not chapters:
        raise GroundingError("chapters must not be empty")
    previous = 0
    for index, raw in enumerate(chapters):
        raw = _mapping(raw, f"chapters[{index}]")
        _require_exact_keys(raw, {"first_segment", "last_segment"}, f"chapters[{index}]")
        first = _segment_number(raw.get("first_segment"), segments, f"chapters[{index}].first_segment")
        last = _segment_number(raw.get("last_segment"), segments, f"chapters[{index}].last_segment")
        if first <= previous or last < first:
            raise GroundingError(f"chapters[{index}] is overlapping, unordered, or invalid")
        previous = last

    actions = _list(draft.get("actions"), "actions")
    if len(actions) > 12:
        raise GroundingError("actions must contain at most 12 entries")
    action_segments: list[int] = []
    for index, action in enumerate(actions):
        _validate_action(action, segments, f"actions[{index}]")
        action_segments.append(action["segment"])
    if len(action_segments) != len(set(action_segments)):
        raise GroundingError("actions contains a duplicate segment")
    claim_count += len(actions)
    return ValidatedDraft(draft, segments, claim_count, claim_count)


def validate(transcript: Any, draft: Any, recording_id: str) -> ValidatedDraft:
    """Validate an official transcript object. Intended for unit callers."""
    recording_id = _text(recording_id, "recording_id", minimum=8, maximum=160)
    return _validate_draft(draft, recording_id, _segments(transcript, recording_id))


def validate_source(transcript_bytes: bytes, metadata: Any, draft: Any, recording_id: str) -> ValidatedDraft:
    """Validate either collector-approved source representation and its selector draft."""
    recording_id = _text(recording_id, "recording_id", minimum=8, maximum=160)
    return _validate_draft(draft, recording_id, _load_segments(transcript_bytes, metadata, recording_id))


def _citation(segment: Segment) -> str:
    if segment.timed:
        return f"{segment.speaker}, {_timestamp(segment.start_ms)}-{_timestamp(segment.end_ms)}, segment {segment.number}"
    return f"unattributed source block {segment.number}"


def _render_segment(segment: Segment) -> str:
    return f"{_normalize(segment.content)} ({_citation(segment)})"


def _selected(validated: ValidatedDraft, section: str) -> Iterable[Segment]:
    for number in validated.raw[section]:
        yield validated.segments[number - 1]


def render_summary(validated: ValidatedDraft) -> bytes:
    draft = validated.raw
    classification = draft["classification"]
    alternatives = ", ".join(f"`{row['type']}` ({row['confidence']:.2f})" for row in classification["alternatives"]) or "none"
    basis = validated.segments[classification["basis_segment"] - 1]
    purpose = validated.segments[draft["purpose_segment"] - 1]
    lines = [
        "**Classification**",
        "",
        f"- Type: `{classification['type']}`",
        f"- Confidence: {classification['confidence']:.2f}",
        f"- Alternatives: {alternatives}",
        f"- Review flag: {'true' if classification['review_flag'] else 'false'}",
        f"- Source basis: {_render_segment(basis)}",
        "",
        "**Purpose / Gist Source Excerpt**",
        "",
        (
            f"See the classification source basis above (segment {purpose.number})."
            if purpose.number == basis.number
            else _render_segment(purpose)
        ),
        "",
        "**Highlights (Source Excerpts)**",
        "",
    ]
    lines.extend(f"- {_render_segment(segment)}" for segment in _selected(validated, "highlights"))
    lines.extend(["", "**Topic Chapters**", ""])
    for chapter in draft["chapters"]:
        first = validated.segments[chapter["first_segment"] - 1]
        last = validated.segments[chapter["last_segment"] - 1]
        if first.timed and last.timed:
            prefix = f"{_timestamp(first.start_ms)}-{_timestamp(last.end_ms)}"
        else:
            prefix = "unattributed source"
        lines.append(f"- {prefix} - segments {first.number}-{last.number}")
    section_titles = {
        "decisions": "Explicit Decision Excerpts",
        "open_questions": "Open Question / Blocker Excerpts",
        "risks": "Risk / Concern Excerpts",
        "uncertainties": "Uncertainty Excerpts",
        "transcript_quality": "Transcript Quality Excerpts",
    }
    for section, title in section_titles.items():
        lines.extend(["", f"**{title}**", ""])
        rows = list(_selected(validated, section))
        lines.extend(f"- {_render_segment(segment)}" for segment in rows)
        if not rows:
            lines.append("- None selected from the transcript.")
    lines.extend(["", "**Explicit Elliott Commitments / Actions**", ""])
    if draft["actions"]:
        for row in draft["actions"]:
            lines.append(f"- {_render_segment(validated.segments[row['segment'] - 1])}")
    else:
        lines.append("- No explicit Elliott-owned commitment was identified in the transcript.")
    return ("\n".join(lines) + "\n").encode()


def _action_name(segment: Segment) -> str:
    prefix = "Plaud follow-up: "
    content = _normalize(segment.content)
    limit = 240 - len(prefix)
    if len(content) > limit:
        content = content[: limit - 3].rsplit(" ", 1)[0] + "..."
    return prefix + content


def render_grounded_actions(validated: ValidatedDraft) -> bytes:
    actions = []
    for row in validated.raw["actions"]:
        segment = validated.segments[row["segment"] - 1]
        actions.append({
            "name": _action_name(segment),
            "state": row["state"],
            "explicit_elliott_owned": True,
            "claim": _normalize(segment.content),
            "citation": _citation(segment),
        })
    return _canonical({"version": 2, "recording_id": validated.raw["recording_id"], "actions": actions})


def render_source_index(segments: Sequence[Segment]) -> bytes:
    lines = ["Source-extractive Plaud grounding index", ""]
    for segment in segments:
        if segment.timed:
            label = f"segment {segment.number} | {segment.speaker} | {_timestamp(segment.start_ms)}-{_timestamp(segment.end_ms)}"
        else:
            label = f"source block {segment.number} | Unknown"
        lines.append(f"[{label}]")
        lines.extend(textwrap.wrap(_normalize(segment.content), width=100, break_long_words=False, break_on_hyphens=False))
        lines.append("")
    return ("\n".join(lines) + "\n").encode()


def _write_new(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        raise GroundingError(f"refusing to overwrite {path.name}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_cli_paths(args: argparse.Namespace) -> None:
    expected = {
        "transcript_file": "raw-transcript",
        "source_metadata_file": "source-metadata.json",
    }
    if args.command == "index-source":
        expected["source_output"] = "grounding-source.txt"
    elif args.command == "render":
        expected.update({
            "source_index_file": "grounding-source.txt",
            "draft_file": "grounding-draft.json",
            "summary_output": "summary.md",
            "actions_output": "grounded-actions.json",
            "receipt_output": "grounding-receipt.json",
        })
    else:
        expected.update({
            "source_index_file": "grounding-source.txt",
            "draft_file": "grounding-draft.json",
            "receipt_file": "grounding-receipt.json",
            "plan_output": "action-plan.json",
        })
    work = args.transcript_file.parent.resolve(strict=True)
    root = WORK_ROOT.resolve(strict=True)
    if work.parent != root:
        raise GroundingError("artifacts must be in one direct Plaud processing work directory")
    metadata = work.stat(follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700 or metadata.st_uid != os.getuid():
        raise GroundingError("Plaud processing work directory must be owner-only")
    for attribute, name in expected.items():
        path = getattr(args, attribute)
        if path.name != name or path.parent.resolve(strict=True) != work:
            raise GroundingError(f"{attribute} must be exact work artifact {name}")


def index_source(args: argparse.Namespace) -> dict[str, Any]:
    transcript_bytes = _read_private(args.transcript_file, "transcript")
    source_metadata, source_metadata_bytes = _load_json(args.source_metadata_file, "source metadata")
    recording_id = _text(args.recording_id, "recording_id", minimum=8, maximum=160)
    segments = _load_segments(transcript_bytes, source_metadata, recording_id)
    index = render_source_index(segments)
    _write_new(args.source_output, index)
    return {
        "schema": "plaud-grounding-source-index-v1",
        "status": "indexed",
        "recording_id": recording_id,
        "transcript_sha256": _sha(transcript_bytes),
        "source_metadata_sha256": _sha(source_metadata_bytes),
        "source_index_sha256": _sha(index),
        "segment_count": len(segments),
        "provider_operations": 0,
    }


def render(args: argparse.Namespace) -> dict[str, Any]:
    transcript_bytes = _read_private(args.transcript_file, "transcript")
    source_metadata, source_metadata_bytes = _load_json(args.source_metadata_file, "source metadata")
    draft, draft_bytes = _load_json(args.draft_file, "draft")
    validated = validate_source(transcript_bytes, source_metadata, draft, args.recording_id)
    source_index = _read_private(args.source_index_file, "source index")
    if source_index != render_source_index(validated.segments):
        raise GroundingError("source index does not bind the current transcript")
    summary = render_summary(validated)
    actions = render_grounded_actions(validated)
    for path in (args.summary_output, args.actions_output, args.receipt_output):
        if path.exists():
            raise GroundingError(f"refusing to overwrite {path.name}")
    receipt = {
        "schema": "plaud-grounding-gate-v2",
        "status": "validated",
        "recording_id": args.recording_id,
        "transcript_sha256": _sha(transcript_bytes),
        "source_metadata_sha256": _sha(source_metadata_bytes),
        "source_index_sha256": _sha(source_index),
        "draft_sha256": _sha(draft_bytes),
        "summary_sha256": _sha(summary),
        "grounded_actions_sha256": _sha(actions),
        "segment_count": len(validated.segments),
        "claim_count": validated.claim_count,
        "evidence_excerpt_count": validated.evidence_count,
        "rendering": "whole-source-segments",
        "provider_operations": 0,
    }
    _write_new(args.summary_output, summary)
    _write_new(args.actions_output, actions)
    _write_new(args.receipt_output, _canonical(receipt))
    return receipt


def finalize_actions(args: argparse.Namespace) -> dict[str, Any]:
    transcript_bytes = _read_private(args.transcript_file, "transcript")
    source_metadata, source_metadata_bytes = _load_json(args.source_metadata_file, "source metadata")
    draft, draft_bytes = _load_json(args.draft_file, "draft")
    receipt, _receipt_bytes = _load_json(args.receipt_file, "grounding receipt")
    validated = validate_source(transcript_bytes, source_metadata, draft, args.recording_id)
    source_index = _read_private(args.source_index_file, "source index")
    if source_index != render_source_index(validated.segments):
        raise GroundingError("source index does not bind the current transcript")
    summary = render_summary(validated)
    actions = render_grounded_actions(validated)
    expected = {
        "schema": "plaud-grounding-gate-v2",
        "status": "validated",
        "recording_id": args.recording_id,
        "transcript_sha256": _sha(transcript_bytes),
        "source_metadata_sha256": _sha(source_metadata_bytes),
        "source_index_sha256": _sha(source_index),
        "draft_sha256": _sha(draft_bytes),
        "summary_sha256": _sha(summary),
        "grounded_actions_sha256": _sha(actions),
        "segment_count": len(validated.segments),
        "claim_count": validated.claim_count,
        "evidence_excerpt_count": validated.evidence_count,
        "rendering": "whole-source-segments",
        "provider_operations": 0,
    }
    if receipt != expected:
        raise GroundingError("grounding receipt does not bind the current source and outputs")
    if not UUID_RE.fullmatch(args.note_id):
        raise GroundingError("note_id must be an Evernote GUID")
    plan_actions = []
    for row in json.loads(actions)["actions"]:
        plan_actions.append({
            "name": row["name"],
            "note": (
                f"Evernote note: {args.note_id}. Plaud recording: {args.recording_id}. "
                f"Exact source commitment: {row['claim']} ({row['citation']})."
            ),
            "state": row["state"],
            "explicit_elliott_owned": True,
        })
    plan = _canonical({"version": 1, "recording_id": args.recording_id, "actions": plan_actions})
    _write_new(args.plan_output, plan)
    return {
        "schema": "plaud-grounded-action-plan-v1",
        "status": "finalized",
        "recording_id": args.recording_id,
        "note_id": args.note_id,
        "action_count": len(plan_actions),
        "action_plan_sha256": _sha(plan),
        "provider_operations": 0,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    index_parser = commands.add_parser("index-source")
    render_parser = commands.add_parser("render")
    finalize_parser = commands.add_parser("finalize-actions")
    for command in (index_parser, render_parser, finalize_parser):
        command.add_argument("--recording-id", required=True)
        command.add_argument("--transcript-file", type=Path, required=True)
        command.add_argument("--source-metadata-file", type=Path, required=True)
    index_parser.add_argument("--source-output", type=Path, required=True)
    for command in (render_parser, finalize_parser):
        command.add_argument("--source-index-file", type=Path, required=True)
        command.add_argument("--draft-file", type=Path, required=True)
    render_parser.add_argument("--summary-output", type=Path, required=True)
    render_parser.add_argument("--actions-output", type=Path, required=True)
    render_parser.add_argument("--receipt-output", type=Path, required=True)
    finalize_parser.add_argument("--receipt-file", type=Path, required=True)
    finalize_parser.add_argument("--note-id", required=True)
    finalize_parser.add_argument("--plan-output", type=Path, required=True)
    return result


def main() -> int:
    os.umask(0o077)
    args = parser().parse_args()
    try:
        _validate_cli_paths(args)
        if args.command == "index-source":
            value = index_source(args)
        elif args.command == "render":
            value = render(args)
        else:
            value = finalize_actions(args)
    except GroundingError as exc:
        print(json.dumps({"ok": False, "error": "GroundingError", "message": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps({"ok": True, **value}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
