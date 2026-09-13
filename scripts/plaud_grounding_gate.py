#!/usr/bin/env python3
"""Validate Plaud summary claims before any provider-facing workflow step.

The model writes a structured draft whose claims cite exact transcript excerpts.
This tool validates that draft and deterministically renders the summary and
action candidates.  It intentionally has no provider, registry, or delivery
surface.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
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
CLAIM_SECTIONS = (
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
    "purpose",
    "highlights",
    "chapters",
    "decisions",
    "actions",
    "open_questions",
    "risks",
    "uncertainties",
    "transcript_quality",
}
STOPWORDS = {
    "a", "about", "after", "again", "all", "also", "an", "and", "any", "are", "as", "at",
    "be", "because", "been", "before", "being", "between", "both", "but", "by", "can", "could",
    "did", "do", "does", "during", "each", "for", "from", "had", "has", "have", "he", "her",
    "here", "him", "his", "how", "i", "if", "in", "into", "is", "it", "its", "just", "may",
    "more", "most", "no", "not", "of", "on", "one", "only", "or", "other", "our", "out", "said",
    "she", "so", "some", "than", "that", "the", "their", "them", "there", "they", "this", "those",
    "through", "to", "up", "was", "we", "were", "what", "when", "where", "which", "while", "who",
    "will", "with", "would", "you", "your",
}
COMMITMENT_RE = re.compile(
    r"\b(?:i\s+will|i['\u2019]ll|i\s+am\s+going\s+to|i['\u2019]m\s+going\s+to|let\s+me|i\s+can)\b",
    re.IGNORECASE,
)
WEAK_COMMITMENT_RE = re.compile(
    r"\b(?:maybe|might|perhaps|probably|possibly|if)\b(?:\W+\w+){0,5}\W+"
    r"(?:i\s+will|i['\u2019]ll|i\s+am\s+going\s+to|i['\u2019]m\s+going\s+to|let\s+me|i\s+can)\b",
    re.IGNORECASE,
)
WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['\u2019][A-Za-z0-9]+)?")
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
    return " ".join(value.split()).casefold()


def _tokens(value: str) -> set[str]:
    return {token.casefold() for token in WORD_RE.findall(value) if token.casefold() not in STOPWORDS and len(token) > 1}


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
    blocks = [" ".join(block.split()) for block in re.split(r"\n\s*\n|(?<=\.)\s*\n", text) if block.strip()]
    if not blocks or any(len(block) > 10000 for block in blocks):
        raise GroundingError("fallback transcript has no bounded source blocks")
    return tuple(Segment(index, "Unknown", block, 0, 0, False) for index, block in enumerate(blocks, 1))


def _evidence(raw: Any, segments: Sequence[Segment], label: str) -> tuple[tuple[Segment, str], ...]:
    rows = _list(raw, f"{label}.evidence")
    if not 1 <= len(rows) <= 8:
        raise GroundingError(f"{label}.evidence must contain 1..8 excerpts")
    result: list[tuple[Segment, str]] = []
    seen: set[tuple[int, str]] = set()
    for index, item in enumerate(rows):
        item = _mapping(item, f"{label}.evidence[{index}]")
        _require_exact_keys(item, {"segment", "quote"}, f"{label}.evidence[{index}]")
        number = _integer(item.get("segment"), f"{label}.evidence[{index}].segment", minimum=1)
        if number > len(segments):
            raise GroundingError(f"{label}.evidence[{index}] references a missing segment")
        quote = _text(item.get("quote"), f"{label}.evidence[{index}].quote", minimum=8, maximum=1200)
        segment = segments[number - 1]
        if _normalize(quote) not in _normalize(segment.content):
            raise GroundingError(f"{label}.evidence[{index}] is not an exact excerpt of segment {number}")
        key = number, _normalize(quote)
        if key in seen:
            raise GroundingError(f"{label}.evidence contains a duplicate excerpt")
        seen.add(key)
        result.append((segment, quote))
    return tuple(result)


def _validate_lexical_grounding(text: str, evidence: Sequence[tuple[Segment, str]], label: str) -> None:
    claim_tokens = _tokens(text)
    evidence_tokens = _tokens(" ".join(quote for _segment, quote in evidence))
    overlap = claim_tokens & evidence_tokens
    required = min(4, max(2, (len(claim_tokens) + 2) // 3))
    if len(overlap) < required:
        raise GroundingError(f"{label} is not lexically grounded in its cited excerpts")
    numbers = {token for token in WORD_RE.findall(text) if token.isdigit()}
    if not numbers.issubset({token for token in WORD_RE.findall(" ".join(q for _s, q in evidence)) if token.isdigit()}):
        raise GroundingError(f"{label} adds a number absent from its cited excerpts")


def _claim(raw: Any, segments: Sequence[Segment], label: str) -> tuple[Mapping[str, Any], tuple[tuple[Segment, str], ...]]:
    raw = _mapping(raw, label)
    _require_exact_keys(raw, {"text", "evidence"}, label)
    text = _text(raw.get("text"), f"{label}.text", minimum=8, maximum=800)
    evidence = _evidence(raw.get("evidence"), segments, label)
    _validate_lexical_grounding(text, evidence, label)
    return raw, evidence


def _validate_classification(raw: Any, segments: Sequence[Segment]) -> tuple[int, int]:
    raw = _mapping(raw, "classification")
    _require_exact_keys(raw, {"type", "confidence", "alternatives", "review_flag", "basis"}, "classification")
    kind = _text(raw.get("type"), "classification.type", maximum=40)
    confidence = raw.get("confidence")
    if kind not in ALLOWED_TYPES or isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise GroundingError("classification type or confidence is invalid")
    alternatives = _list(raw.get("alternatives"), "classification.alternatives")
    seen = {kind}
    for index, item in enumerate(alternatives):
        item = _mapping(item, f"classification.alternatives[{index}]")
        _require_exact_keys(item, {"type", "confidence"}, f"classification.alternatives[{index}]")
        alt_kind = _text(item.get("type"), f"classification.alternatives[{index}].type", maximum=40)
        alt_confidence = item.get("confidence")
        if alt_kind not in ALLOWED_TYPES or alt_kind in seen or isinstance(alt_confidence, bool) or not isinstance(alt_confidence, (int, float)) or not 0 <= alt_confidence <= 1:
            raise GroundingError("classification alternative is invalid")
        seen.add(alt_kind)
    if not isinstance(raw.get("review_flag"), bool):
        raise GroundingError("classification.review_flag must be boolean")
    _claim(raw.get("basis"), segments, "classification.basis")
    return 1, len(raw["basis"]["evidence"])


def _validate_action(raw: Any, segments: Sequence[Segment], label: str) -> tuple[int, int]:
    raw = _mapping(raw, label)
    _require_exact_keys(raw, {"name", "state", "explicit_elliott_owned", "claim"}, label)
    name = _text(raw.get("name"), f"{label}.name", minimum=3, maximum=240)
    state = _text(raw.get("state"), f"{label}.state", maximum=20)
    if state not in ALLOWED_STATES or raw.get("explicit_elliott_owned") is not True:
        raise GroundingError(f"{label} state or ownership marker is invalid")
    _raw_claim, evidence = _claim(raw.get("claim"), segments, f"{label}.claim")
    owner_quotes = [quote for segment, quote in evidence if segment.speaker.casefold() == "elliott"]
    if not owner_quotes:
        raise GroundingError(f"{label} has no Elliott-spoken evidence")
    explicit = [quote for quote in owner_quotes if COMMITMENT_RE.search(quote) and not WEAK_COMMITMENT_RE.search(quote)]
    if not explicit:
        raise GroundingError(f"{label} has no unconditional first-person Elliott commitment")
    _validate_lexical_grounding(name, [(segments[0], quote) for quote in explicit], f"{label}.name")
    return 1, len(evidence)


def _validate_draft(draft: Any, recording_id: str, segments: tuple[Segment, ...]) -> ValidatedDraft:
    draft = _mapping(draft, "draft")
    _require_exact_keys(draft, TOP_LEVEL_KEYS, "draft")
    if draft.get("version") != 1 or draft.get("recording_id") != recording_id:
        raise GroundingError("draft version or recording_id is invalid")

    claim_count, evidence_count = _validate_classification(draft.get("classification"), segments)
    _claim(draft.get("purpose"), segments, "purpose")
    claim_count += 1
    evidence_count += len(draft["purpose"]["evidence"])

    highlights = _list(draft.get("highlights"), "highlights")
    if not 3 <= len(highlights) <= 5:
        raise GroundingError("highlights must contain 3..5 claims")
    for section in CLAIM_SECTIONS:
        rows = _list(draft.get(section), section)
        if section in {"uncertainties", "transcript_quality"} and not rows:
            raise GroundingError(f"{section} must contain at least one claim")
        for index, item in enumerate(rows):
            _claim(item, segments, f"{section}[{index}]")
            claim_count += 1
            evidence_count += len(item["evidence"])

    chapters = _list(draft.get("chapters"), "chapters")
    if not chapters:
        raise GroundingError("chapters must not be empty")
    previous = 0
    for index, raw in enumerate(chapters):
        raw = _mapping(raw, f"chapters[{index}]")
        _require_exact_keys(raw, {"title", "first_segment", "last_segment"}, f"chapters[{index}]")
        title = _text(raw.get("title"), f"chapters[{index}].title", minimum=3, maximum=160)
        first = _integer(raw.get("first_segment"), f"chapters[{index}].first_segment", minimum=1)
        last = _integer(raw.get("last_segment"), f"chapters[{index}].last_segment", minimum=first)
        if first <= previous or last > len(segments):
            raise GroundingError(f"chapters[{index}] is overlapping, unordered, or out of range")
        previous = last
        span = " ".join(segment.content for segment in segments[first - 1:last])
        _validate_lexical_grounding(title, [(segments[first - 1], span)], f"chapters[{index}].title")

    actions = _list(draft.get("actions"), "actions")
    for index, action in enumerate(actions):
        claims, excerpts = _validate_action(action, segments, f"actions[{index}]")
        claim_count += claims
        evidence_count += excerpts
    return ValidatedDraft(draft, segments, claim_count, evidence_count)


def validate(transcript: Any, draft: Any, recording_id: str) -> ValidatedDraft:
    """Validate an official transcript object. Intended for unit callers."""
    recording_id = _text(recording_id, "recording_id", minimum=8, maximum=160)
    return _validate_draft(draft, recording_id, _segments(transcript, recording_id))


def validate_source(
    transcript_bytes: bytes,
    metadata: Any,
    draft: Any,
    recording_id: str,
) -> ValidatedDraft:
    """Validate either collector-approved source representation."""
    recording_id = _text(recording_id, "recording_id", minimum=8, maximum=160)
    schema, kind = _source_metadata(metadata, transcript_bytes, recording_id)
    if kind == "official_transcript":
        try:
            transcript = json.loads(transcript_bytes)
        except json.JSONDecodeError as exc:
            raise GroundingError("official transcript is malformed") from exc
        segments = _segments(transcript, recording_id)
        if transcript["schema"] != schema:
            raise GroundingError("official transcript schema differs from source metadata")
    else:
        segments = _fallback_segments(transcript_bytes)
    return _validate_draft(draft, recording_id, segments)


def _citation(evidence: Iterable[tuple[Segment, str]]) -> str:
    parts = []
    for segment, _quote in evidence:
        if segment.timed:
            parts.append(f"{segment.speaker}, {_timestamp(segment.start_ms)}-{_timestamp(segment.end_ms)}, segment {segment.number}")
        else:
            parts.append(f"unattributed source block {segment.number}")
    return "; ".join(parts)


def _render_claim(raw: Mapping[str, Any], validated: ValidatedDraft) -> str:
    evidence = _evidence(raw["evidence"], validated.segments, "render")
    return f"{raw['text']} ({_citation(evidence)})"


def render_summary(validated: ValidatedDraft) -> bytes:
    draft = validated.raw
    classification = draft["classification"]
    alternatives = ", ".join(f"`{row['type']}` ({row['confidence']:.2f})" for row in classification["alternatives"]) or "none"
    lines = [
        "**Classification**",
        "",
        f"- Type: `{classification['type']}`",
        f"- Confidence: {classification['confidence']:.2f}",
        f"- Alternatives: {alternatives}",
        f"- Review flag: {'true' if classification['review_flag'] else 'false'}",
        f"- Basis: {_render_claim(classification['basis'], validated)}",
        "",
        "**Purpose / Gist**",
        "",
        _render_claim(draft["purpose"], validated),
        "",
        "**Highlights**",
        "",
    ]
    lines.extend(f"- {_render_claim(row, validated)}" for row in draft["highlights"])
    lines.extend(["", "**Topic Chapters**", ""])
    for chapter in draft["chapters"]:
        first = validated.segments[chapter["first_segment"] - 1]
        last = validated.segments[chapter["last_segment"] - 1]
        if first.timed and last.timed:
            prefix = f"{_timestamp(first.start_ms)}-{_timestamp(last.end_ms)}"
        else:
            prefix = f"source blocks {first.number}-{last.number}"
        lines.append(f"- {prefix} - {chapter['title']} (segments {first.number}-{last.number})")
    section_titles = {
        "decisions": "Explicit Decisions",
        "open_questions": "Open Questions / Blockers",
        "risks": "Risks",
        "uncertainties": "Uncertainty",
        "transcript_quality": "Transcript Quality Notes",
    }
    for section, title in section_titles.items():
        lines.extend(["", f"**{title}**", ""])
        rows = draft[section]
        if rows:
            lines.extend(f"- {_render_claim(row, validated)}" for row in rows)
        else:
            lines.append("- None explicitly stated in the transcript.")
    lines.extend(["", "**Explicit Commitments / Actions**", ""])
    if draft["actions"]:
        lines.extend(f"- {_render_claim(row['claim'], validated)}" for row in draft["actions"])
    else:
        lines.append("- No explicit Elliott-owned commitment was identified in the transcript.")
    return ("\n".join(lines) + "\n").encode()


def render_grounded_actions(validated: ValidatedDraft) -> bytes:
    actions = []
    for row in validated.raw["actions"]:
        evidence = _evidence(row["claim"]["evidence"], validated.segments, "action-render")
        actions.append({
            "name": row["name"],
            "state": row["state"],
            "explicit_elliott_owned": True,
            "claim": row["claim"]["text"],
            "citation": _citation(evidence),
        })
    return _canonical({"version": 1, "recording_id": validated.raw["recording_id"], "actions": actions})


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
        "draft_file": "grounding-draft.json",
    }
    if args.command == "render":
        expected.update({
            "summary_output": "summary.md",
            "actions_output": "grounded-actions.json",
            "receipt_output": "grounding-receipt.json",
        })
    else:
        expected.update({"receipt_file": "grounding-receipt.json", "plan_output": "action-plan.json"})
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


def render(args: argparse.Namespace) -> dict[str, Any]:
    transcript_bytes = _read_private(args.transcript_file, "transcript")
    source_metadata, source_metadata_bytes = _load_json(args.source_metadata_file, "source metadata")
    draft, draft_bytes = _load_json(args.draft_file, "draft")
    validated = validate_source(transcript_bytes, source_metadata, draft, args.recording_id)
    summary = render_summary(validated)
    actions = render_grounded_actions(validated)
    for path in (args.summary_output, args.actions_output, args.receipt_output):
        if path.exists():
            raise GroundingError(f"refusing to overwrite {path.name}")
    receipt = {
        "schema": "plaud-grounding-gate-v1",
        "status": "validated",
        "recording_id": args.recording_id,
        "transcript_sha256": _sha(transcript_bytes),
        "source_metadata_sha256": _sha(source_metadata_bytes),
        "draft_sha256": _sha(draft_bytes),
        "summary_sha256": _sha(summary),
        "grounded_actions_sha256": _sha(actions),
        "segment_count": len(validated.segments),
        "claim_count": validated.claim_count,
        "evidence_excerpt_count": validated.evidence_count,
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
    summary = render_summary(validated)
    actions = render_grounded_actions(validated)
    expected = {
        "schema": "plaud-grounding-gate-v1",
        "status": "validated",
        "recording_id": args.recording_id,
        "transcript_sha256": _sha(transcript_bytes),
        "source_metadata_sha256": _sha(source_metadata_bytes),
        "draft_sha256": _sha(draft_bytes),
        "summary_sha256": _sha(summary),
        "grounded_actions_sha256": _sha(actions),
        "segment_count": len(validated.segments),
        "claim_count": validated.claim_count,
        "evidence_excerpt_count": validated.evidence_count,
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
                f"Grounded commitment: {row['claim']} ({row['citation']})."
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
    render_parser = commands.add_parser("render")
    finalize_parser = commands.add_parser("finalize-actions")
    for command in (render_parser, finalize_parser):
        command.add_argument("--recording-id", required=True)
        command.add_argument("--transcript-file", type=Path, required=True)
        command.add_argument("--source-metadata-file", type=Path, required=True)
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
        value = render(args) if args.command == "render" else finalize_actions(args)
    except GroundingError as exc:
        print(json.dumps({"ok": False, "error": "GroundingError", "message": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps({"ok": True, **value}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
