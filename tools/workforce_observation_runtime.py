"""Turn-local binding between bounded observations and workforce signals."""

from __future__ import annotations

from contextvars import ContextVar
import hashlib
import json
from typing import Any, Iterable


_ACTIVE_BUZZ_REFS: ContextVar[dict[str, frozenset[str]]] = ContextVar(
    "workforce_observed_buzz_refs", default={}
)


def clear_buzz_events() -> None:
    """Remove any observation bindings inherited by the current context."""
    _ACTIVE_BUZZ_REFS.set({})
    from tools.workforce_signal_runtime import replace_buzz_refs

    replace_buzz_refs({})


def _buzz_dedupe_ref(
    *, room_id: str, author: str, content_sha256: str
) -> str:
    canonical = json.dumps(
        {
            "room_id": room_id.strip(),
            "author": author.strip() or "unknown",
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
        room_id = str(event.get("room_id") or "").strip()
        event_id = str(event.get("event_id") or "").strip()
        author = str(event.get("author") or "unknown").strip() or "unknown"
        content = str(event.get("content") or "").strip()
        content_sha256 = str(event.pop("_full_content_sha256", "")).strip()
        if not room_id or not event_id or not content:
            continue
        if (
            len(content_sha256) != 64
            or any(value not in "0123456789abcdef" for value in content_sha256)
        ):
            content_sha256 = hashlib.sha256(content.encode()).hexdigest()
        dedupe_ref = _buzz_dedupe_ref(
            room_id=room_id, author=author, content_sha256=content_sha256
        )
        evidence_ref = f"buzz:event:{event_id}"
        event["dedupe_ref"] = dedupe_ref
        event["evidence_ref"] = evidence_ref
        bindings.setdefault(dedupe_ref, set()).add(evidence_ref)
    frozen = {key: frozenset(values) for key, values in bindings.items()}
    _ACTIVE_BUZZ_REFS.set(frozen)
    from tools.workforce_signal_runtime import replace_buzz_refs

    replace_buzz_refs(frozen)


def validate_buzz_signal_binding(
    *, dedupe_ref: str, evidence_references: Iterable[Any]
) -> str:
    """Return a current observed ref or reject missing, stale, and forged input."""
    candidate = str(dedupe_ref or "").strip()
    if not candidate:
        raise ValueError("dedupe_ref is required for a bounded Buzz observation")
    from tools.workforce_signal_runtime import current_buzz_refs

    bindings = current_buzz_refs() or _ACTIVE_BUZZ_REFS.get()
    allowed_evidence = bindings.get(candidate)
    if not allowed_evidence:
        raise ValueError("dedupe_ref was not returned by this turn's Buzz observation")
    supplied = {
        str(value).strip() for value in evidence_references if str(value).strip()
    }
    if not supplied.intersection(allowed_evidence):
        raise ValueError(
            "evidence_references must include the observed event's evidence_ref"
        )
    return candidate
