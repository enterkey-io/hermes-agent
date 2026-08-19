"""Proactive workforce outcome-control plugin.

The plugin is deliberately inert until an operator initializes its additive
Kanban tables and selects a runtime mode. Merely installing or loading it does
not create cards, change task state, or start a background observer.
"""

from __future__ import annotations

import logging

from hermes_cli import kanban_db
from plugins.workforce_control.store import observe_dispatch_tick
from plugins.workforce_control.tools import TOOLS


logger = logging.getLogger(__name__)


def _on_dispatch_tick(*, board=None, dry_run=False, **_kwargs) -> None:
    if dry_run:
        return
    try:
        with kanban_db.connect_closing(board=board) as conn:
            observe_dispatch_tick(conn)
    except Exception as exc:  # observer failure must never stall dispatch
        logger.debug("workforce-control observer degraded safely: %s", exc)


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
