"""Turn-local binding between bounded observations and workforce signals."""

from __future__ import annotations

from contextvars import ContextVar
import hashlib
import json
from typing import Any, Callable, Iterable


_ACTIVE_BUZZ_REFS: ContextVar[dict[str, frozenset[str]]] = ContextVar(
    "workforce_observed_buzz_refs", default={}
)


def clear_buzz_events() -> None:
    """Remove any observation bindings inherited by the current context."""
    _ACTIVE_BUZZ_REFS.set({})
    from tools.workforce_signal_runtime import replace_buzz_refs

    replace_buzz_refs({})


def _buzz_dedupe_ref(
    *, room_id: str, author_id: str, content_sha256: str
) -> str:
    canonical = json.dumps(
        {
            "room_id": room_id.strip(),
            "author_id": author_id.strip(),
            "content_sha256": content_sha256,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"buzz-content:{hashlib.sha256(canonical.encode()).hexdigest()}"


def bind_buzz_events(events: Iterable[dict[str, Any]]) -> None:
    """Annotate observed events and replace the current turn's valid bindings."""
    bindings: dict[str, set[str]] = {}
    for event in events:
        event.pop("dedupe_ref", None)
        event.pop("evidence_ref", None)
        event.pop("binding_error", None)
        room_id = str(event.get("room_id") or "").strip()
        event_id = str(event.get("event_id") or "").strip()
        author_id = str(event.get("author_id") or "").strip().casefold()
        content = str(event.get("content") or "").strip()
        content_sha256 = str(event.pop("_full_content_sha256", "")).strip()
        valid_author = (
            len(author_id) == 64
            and all(value in "0123456789abcdef" for value in author_id)
        )
        if not room_id or not event_id or not valid_author or not content:
            event["binding_error"] = "stable Buzz event identity unavailable"
            continue
        event["author_id"] = author_id
        if (
            len(content_sha256) != 64
            or any(value not in "0123456789abcdef" for value in content_sha256)
        ):
            content_sha256 = hashlib.sha256(content.encode()).hexdigest()
        dedupe_ref = _buzz_dedupe_ref(
            room_id=room_id, author_id=author_id, content_sha256=content_sha256
        )
        evidence_ref = f"buzz:event:{event_id}"
        event["dedupe_ref"] = dedupe_ref
        event["evidence_ref"] = evidence_ref
        bindings.setdefault(dedupe_ref, set()).add(evidence_ref)
    frozen = {key: frozenset(values) for key, values in bindings.items()}
    _ACTIVE_BUZZ_REFS.set(frozen)
    from tools.workforce_signal_runtime import replace_buzz_refs

    replace_buzz_refs(frozen)


def _validate_binding_map(
    bindings: dict[str, frozenset[str]],
    *,
    dedupe_ref: str,
    evidence_references: Iterable[Any],
) -> str:
    candidate = str(dedupe_ref or "").strip()
    if not candidate:
        raise ValueError("dedupe_ref is required for a bounded Buzz observation")
    allowed_evidence = bindings.get(candidate)
    if not allowed_evidence:
        raise ValueError("dedupe_ref was not returned by this turn's Buzz observation")
    supplied = {
        str(value).strip() for value in evidence_references if str(value).strip()
    }
    if not supplied:
        raise ValueError(
            "evidence_references must include the observed event's evidence_ref"
        )
    unexpected = supplied.difference(allowed_evidence)
    if unexpected:
        raise ValueError(
            "evidence_references contain an event outside the selected dedupe_ref"
        )
    return candidate


def validate_buzz_signal_binding(
    *, dedupe_ref: str, evidence_references: Iterable[Any]
) -> str:
    """Return a current observed ref or reject missing, stale, and forged input."""
    from tools.workforce_signal_runtime import active_buzz_refs

    active_bindings = active_buzz_refs()
    return _validate_binding_map(
        active_bindings if active_bindings is not None else _ACTIVE_BUZZ_REFS.get(),
        dedupe_ref=dedupe_ref,
        evidence_references=evidence_references,
    )


def prepare_buzz_signal_commit_guard(
    *, dedupe_ref: str, evidence_references: Iterable[Any]
) -> Callable[[], None]:
    """Return the transaction's nonblocking commit-admission callback.

    The callback is the linearization point: revocation first makes it fail,
    while a successful callback admits the immediately following commit. No
    state lock is retained across SQLite or filesystem I/O.
    """
    from tools.workforce_signal_runtime import active_buzz_commit_reader

    evidence = tuple(evidence_references)
    snapshot, read_at_commit = active_buzz_commit_reader()
    bindings = snapshot if snapshot is not None else _ACTIVE_BUZZ_REFS.get()
    _validate_binding_map(
        bindings,
        dedupe_ref=dedupe_ref,
        evidence_references=evidence,
    )

    def guard() -> None:
        current = read_at_commit()
        _validate_binding_map(
            current if current is not None else _ACTIVE_BUZZ_REFS.get(),
            dedupe_ref=dedupe_ref,
            evidence_references=evidence,
        )

    return guard
