"""Host-observed required-workforce-signal outcome state for one Cron turn."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field


@dataclass
class RequiredSignalState:
    required: bool = False
    track_attempts: bool = False
    attempted: bool = False
    failure: str | None = None
    completed: bool = False
    buzz_refs: dict[str, frozenset[str]] = field(default_factory=dict)
    turn_claimed: bool = False


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
    if state is not None and not state.turn_claimed:
        state.turn_claimed = True
        return None, state
    state = RequiredSignalState(turn_claimed=True)
    return _ACTIVE.set(state), state


def release_turn(token: Token | None, state: RequiredSignalState) -> None:
    """Release a state claimed by :func:`claim_turn`."""
    if token is None:
        state.turn_claimed = False
    else:
        _ACTIVE.reset(token)


def mark_attempted() -> None:
    state = _ACTIVE.get()
    if state is not None and state.track_attempts:
        state.attempted = True


def mark_failure(message: str) -> None:
    state = _ACTIVE.get()
    if state is not None and state.track_attempts and not state.completed:
        state.attempted = True
        state.failure = str(message)[:800]


def mark_success() -> None:
    state = _ACTIVE.get()
    if state is not None and state.track_attempts:
        # A validation-only rejection is recoverable within the same model
        # turn because registry preflight runs before write reservation. Once a
        # later call commits the required signal, that earlier rejection must
        # not poison the host-observed outcome.
        state.failure = None
        state.attempted = True
        state.completed = True


def replace_buzz_refs(bindings: dict[str, frozenset[str]]) -> None:
    """Replace observed refs on the mutable state shared across context copies."""
    state = _ACTIVE.get()
    if state is not None:
        state.buzz_refs = dict(bindings)


def current_buzz_refs() -> dict[str, frozenset[str]]:
    state = _ACTIVE.get()
    return state.buzz_refs if state is not None else {}
