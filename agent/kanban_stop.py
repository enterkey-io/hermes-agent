"""Turn-end guard for kanban workers.

Kanban workers must end with ``kanban_complete``, ``kanban_block``, or a
successful review transition, or an explicitly requested scheduled handoff.
Models (especially GLM / Qwen
families) sometimes narrate the next step
("Let me write the report now") and stop with ``finish_reason=stop`` and no
tool calls. Hermes treats that as a clean exit → ``rc=0`` → dispatcher
``protocol_violation``.

This module is policy-only: when a kanban worker tries to finish without a
verified terminal transition, return a bounded synthetic nudge so the conversation
loop continues instead of exiting.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from collections.abc import Mapping
from typing import Any, Iterable, Optional


_TERMINAL_KANBAN_TOOLS = frozenset(
    {
        "kanban_complete",
        "kanban_block",
        "kanban_request_review",
        "kanban_request_changes",
        "kanban_handoff",
        "kanban_pass_review",
    }
)

_DEFAULT_MAX_ATTEMPTS = 2


def kanban_stop_nudge_enabled() -> bool:
    """Return whether the kanban stop-guard is active for this process.

    On when ``HERMES_KANBAN_TASK`` is set (dispatcher-spawned worker), unless
    ``HERMES_KANBAN_STOP_NUDGE`` explicitly disables it.
    """
    env = os.environ.get("HERMES_KANBAN_STOP_NUDGE")
    if env is not None and env.strip().lower() in {"0", "false", "no", "off"}:
        return False
    task = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    return bool(task)


def _tool_result_payload(content: Any) -> Optional[Mapping[str, Any]]:
    if isinstance(content, Mapping):
        return content
    if not isinstance(content, str):
        return None
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _tool_result_failure_reason(msg: Mapping[str, Any]) -> Optional[str]:
    """Return a concise reason when a tool result reports an operation failure."""
    content = msg.get("content")
    payload = _tool_result_payload(content)
    if payload is not None:
        error = payload.get("error")
        if error:
            return str(error).strip()
        if payload.get("ok") is False:
            return "tool returned ok=false"
        if payload.get("success") is False:
            return "tool returned success=false"
        status = str(payload.get("status") or "").strip().casefold()
        if status in {"error", "failed", "failure"}:
            return f"tool returned status={status}"
        for field in ("exit_code", "returncode"):
            code = payload.get(field)
            if isinstance(code, int) and not isinstance(code, bool) and code != 0:
                meaning = str(payload.get("exit_code_meaning") or "").strip()
                return f"{field}={code}" + (f" ({meaning})" if meaning else "")
        return None
    if isinstance(content, str):
        text = content.strip()
        if text.casefold().startswith(("error:", "failed:", "failure:")):
            return text
    return None


def latest_operational_failure(
    messages: Iterable[dict] | None,
) -> Optional[str]:
    """Return the newest concrete tool failure observed in this worker run."""
    if not messages:
        return None
    for msg in reversed(list(messages)):
        if not isinstance(msg, Mapping) or msg.get("role") != "tool":
            continue
        reason = _tool_result_failure_reason(msg)
        if reason:
            name = str(msg.get("name") or "tool").strip() or "tool"
            return f"{name}: {reason}"
    return None


def _successful_terminal_result(msg: Mapping[str, Any]) -> bool:
    name = str(msg.get("name") or msg.get("tool_name") or "")
    if name not in _TERMINAL_KANBAN_TOOLS:
        return False

    payload = _tool_result_payload(msg.get("content"))
    if payload is None or payload.get("ok") is not True:
        return False

    expected_task = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    result_task = str(payload.get("task_id") or "").strip()
    if expected_task and result_task != expected_task:
        return False

    if name == "kanban_complete":
        return True
    if name in {"kanban_handoff", "kanban_pass_review"}:
        return True

    status = str(payload.get("status") or "").strip().lower()
    if name == "kanban_request_review":
        return status == "review"
    if name == "kanban_request_changes":
        return status == "ready" and bool(str(payload.get("implementer") or "").strip())
    return status in {"blocked", "todo", "triage"}


def session_called_kanban_terminal(messages: Iterable[dict] | None) -> bool:
    """True after the host reports a successful terminal board transition."""
    if not messages:
        return False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "tool" and _successful_terminal_result(msg):
            return True
    return False


def kanban_stop_requires_failure_recovery(
    *,
    messages: Iterable[dict] | None = None,
    attempts: int = 0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
) -> bool:
    """Return whether a surviving tool failure now needs durable recovery."""
    return bool(
        (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
        and (attempts >= max_attempts or not kanban_stop_nudge_enabled())
        and not session_called_kanban_terminal(messages)
        and latest_operational_failure(messages)
    )


def _worker_run_is_scheduled() -> bool:
    """Recognize a scheduled handoff from persisted state, never terminal stdout."""
    task_id = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    run_id = (os.environ.get("HERMES_KANBAN_RUN_ID") or "").strip()
    if not task_id or not run_id.isdecimal() or int(run_id) < 1:
        return False
    from hermes_cli.kanban_db import kanban_db_path

    try:
        # Do not initialize/migrate a missing or legacy board during a stop check.
        uri = kanban_db_path().resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=0.2)) as conn:
            row = conn.execute(
                "SELECT 1 FROM tasks t JOIN task_runs r ON r.task_id = t.id "
                "WHERE t.id = ? AND r.id = ? AND t.status = 'scheduled' "
                "AND t.current_run_id IS NULL AND r.status = 'scheduled' "
                "AND r.outcome = 'scheduled' AND r.ended_at IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM task_runs newer "
                "WHERE newer.task_id = t.id AND newer.id > r.id)",
                (task_id, int(run_id)),
            ).fetchone()
        return row is not None
    except (OSError, sqlite3.Error, ValueError, OverflowError):
        return False


def build_kanban_stop_nudge(
    *,
    messages: Iterable[dict] | None = None,
    attempts: int = 0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    task_id: Optional[str] = None,
) -> Optional[str]:
    """Return a synthetic follow-up when a kanban worker exits without a terminal tool.

    Returns ``None`` when the guard should not fire (not a kanban worker,
    already completed/blocked/sent to review/parked, or nudge budget exhausted).
    """
    if not kanban_stop_nudge_enabled():
        return None
    if session_called_kanban_terminal(messages):
        return None
    if _worker_run_is_scheduled():
        return None
    if attempts >= max_attempts:
        return None

    tid = (task_id or os.environ.get("HERMES_KANBAN_TASK") or "").strip() or "this task"
    failure = latest_operational_failure(messages)
    failure_guidance = ""
    if failure:
        failure_guidance = (
            f"\n\nAn operational failure is still unresolved: {failure}. "
            "Reporting it or promising follow-up is advisory only, not a durable "
            "action. Retry/recover when safe, request changes on this same card, "
            "hand it to the authorized recovery owner, or call `kanban_block` "
            "with the concrete external reason."
        )
    return (
        "[System: You are a Hermes kanban worker. A plain-text reply is NOT a "
        "terminal state for the board.\n\n"
        f"No successful terminal transition was verified for task `{tid}`. Ending now without one "
        "causes a protocol violation (clean exit with no "
        "`kanban_complete` / `kanban_block` / `kanban_request_review`)."
        f"{failure_guidance}\n\n"
        "Do this immediately in your next response — do not narrate intent:\n"
        "1. Finish any remaining deliverable (write the required file(s) now).\n"
        "2. Call `kanban_complete(summary=..., artifacts=[...])` if the work "
        "is done, `kanban_request_review(summary=...)` if source acceptance is "
        "required, OR `kanban_block(reason=...)` if you are blocked. "
        "If the task explicitly requires parking a partial handoff until a manager "
        "release, use `kanban_block(kind=\"scheduled\", reason=...)` and stop after "
        "it succeeds. Never unblock yourself to satisfy this guard.\n\n"
        "Never end a turn with only a promise of future action. Repeated "
        "operational failures route this run through durable recovery rather "
        "than returning advisory prose.]"
    )


__all__ = [
    "build_kanban_stop_nudge",
    "kanban_stop_nudge_enabled",
    "kanban_stop_requires_failure_recovery",
    "latest_operational_failure",
    "session_called_kanban_terminal",
]
