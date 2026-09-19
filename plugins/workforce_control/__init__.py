"""Proactive workforce outcome-control plugin.

The plugin is deliberately inert until an operator initializes its additive
Kanban tables and selects a runtime mode. Merely installing or loading it does
not create cards, change task state, or start a background observer.
"""

from __future__ import annotations

from contextvars import ContextVar
import logging
from typing import Any

from hermes_cli import kanban_db
from plugins.workforce_control.store import observe_dispatch_tick
from plugins.workforce_control.tools import TOOLS


logger = logging.getLogger(__name__)

_TURN_CLAIMS: ContextVar[tuple[tuple[Any, Any], ...]] = ContextVar(
    "workforce_control_turn_claims", default=()
)


def _on_dispatch_tick(*, board=None, dry_run=False, **_kwargs) -> None:
    if dry_run:
        return
    try:
        with kanban_db.connect_closing(board=board) as conn:
            observe_dispatch_tick(conn)
    except Exception as exc:  # observer failure must never stall dispatch
        logger.debug("workforce-control observer degraded safely: %s", exc)


def _on_turn_start(**_kwargs) -> None:
    from tools.workforce_signal_runtime import claim_turn

    token, state = claim_turn()
    _TURN_CLAIMS.set((*_TURN_CLAIMS.get(), (token, state)))


def _on_turn_end(**_kwargs) -> None:
    claims = _TURN_CLAIMS.get()
    if not claims:
        return
    token, state = claims[-1]
    _TURN_CLAIMS.set(claims[:-1])
    from tools.workforce_signal_runtime import release_turn

    release_turn(token, state)


def register(ctx) -> None:
    for name, schema, handler, check_fn, emoji in TOOLS:
        ctx.register_tool(
            name=name,
            toolset="workforce",
            schema=schema,
            handler=handler,
            check_fn=check_fn,
            emoji=emoji,
        )
    ctx.register_hook("on_kanban_dispatch_tick", _on_dispatch_tick)
    ctx.register_hook("on_turn_start", _on_turn_start)
    ctx.register_hook("on_turn_end", _on_turn_end)
