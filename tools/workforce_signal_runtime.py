"""Host-observed required-workforce-signal outcome state for one Cron turn."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
import threading
from typing import Callable


@dataclass
class RequiredSignalState:
    required: bool = False
    track_attempts: bool = False
    attempted: bool = False
    failure: str | None = None
    completed: bool = False
    buzz_refs: dict[str, frozenset[str]] = field(default_factory=dict)
    turn_claimed: bool = False
    closed: bool = False
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)


_ACTIVE: ContextVar[RequiredSignalState | None] = ContextVar(
    "required_workforce_signal", default=None
)


def activate(
    required: bool,
    *,
    observe_attempts: bool = False,
) -> tuple[Token, RequiredSignalState]:
    # The mutable state must always exist before tool-worker contexts are
    # copied. Observation bindings then cross those contexts even when signal
    # attempts are voluntary, while track_attempts preserves the scheduler's
    # existing optional-attempt behavior.
    state = RequiredSignalState(
        required=required,
        track_attempts=required or observe_attempts,
    )
    return _ACTIVE.set(state), state


def reset(token: Token) -> None:
    _ACTIVE.reset(token)


def claim_turn() -> tuple[Token | None, RequiredSignalState]:
    """Provide one mutable signal state shared by every worker in a turn.

    Cron installs its host-observed state before entering the agent.  The first
    conversation turn claims that state; ordinary CLI and gateway turns create
    an equivalent untracked state.  A nested conversation that inherits the
    outer Context receives a fresh state instead of sharing observation refs.
    """
    state = _ACTIVE.get()
    if state is not None:
        with state._lock:
            if not state.closed and not state.turn_claimed:
                state.turn_claimed = True
                return None, state
    state = RequiredSignalState(turn_claimed=True)
    return _ACTIVE.set(state), state


def release_turn(token: Token | None, state: RequiredSignalState) -> None:
    """Release a state claimed by :func:`claim_turn`."""
    with state._lock:
        state.closed = True
        state.turn_claimed = False
    if token is not None:
        _ACTIVE.reset(token)


def mark_attempted() -> None:
    state = _ACTIVE.get()
    if state is not None:
        with state._lock:
            if not state.closed and state.track_attempts:
                state.attempted = True


def mark_failure(message: str) -> None:
    state = _ACTIVE.get()
    if state is not None:
        with state._lock:
            if not state.closed and state.track_attempts and not state.completed:
                state.attempted = True
                state.failure = str(message)[:800]


def mark_success() -> None:
    state = _ACTIVE.get()
    if state is not None:
        with state._lock:
            if not state.closed and state.track_attempts:
                # A validation-only rejection is recoverable within the same
                # model turn because registry preflight runs before write
                # reservation. A later commit clears that earlier rejection.
                state.failure = None
                state.attempted = True
                state.completed = True


def replace_buzz_refs(bindings: dict[str, frozenset[str]]) -> None:
    """Replace observed refs on the mutable state shared across context copies."""
    state = _ACTIVE.get()
    if state is not None:
        with state._lock:
            if not state.closed:
                state.buzz_refs = dict(bindings)


def current_buzz_refs() -> dict[str, frozenset[str]]:
    state = _ACTIVE.get()
    if state is None:
        return {}
    with state._lock:
        return {} if state.closed else dict(state.buzz_refs)


def active_buzz_refs() -> dict[str, frozenset[str]] | None:
    """Return authoritative turn bindings, or ``None`` outside a turn."""
    state = _ACTIVE.get()
    if state is None:
        return None
    with state._lock:
        return {} if state.closed else dict(state.buzz_refs)


def active_buzz_commit_reader() -> tuple[
    dict[str, frozenset[str]] | None,
    Callable[[], dict[str, frozenset[str]] | None],
]:
    """Snapshot bindings and return a short, revocation-aware commit reader."""
    state = _ACTIVE.get()
    if state is None:
        return None, lambda: None
    with state._lock:
        snapshot = {} if state.closed else dict(state.buzz_refs)

    def read_at_commit() -> dict[str, frozenset[str]]:
        with state._lock:
            return {} if state.closed else dict(state.buzz_refs)

    return snapshot, read_at_commit
