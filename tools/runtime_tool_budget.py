"""Host-enforced tool budgets for bounded unattended agent runs.

The budget is carried in a ContextVar so a cron run can install it before the
agent hops to its worker thread.  ToolRegistry is the single enforcement
boundary: prompt instructions cannot enlarge or bypass these limits.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
import threading
from typing import Any


class RuntimeToolBudgetError(PermissionError):
    """Raised when an unattended run exceeds its host-issued tool budget."""


@dataclass
class RuntimeToolBudget:
    max_calls: int
    max_writes: int
    max_detail_reads: int
    max_list_items: int
    allowed_tools: frozenset[str]
    write_tools: frozenset[str]
    tool_call_limits: dict[str, int]
    calls: int = 0
    writes: int = 0
    detail_reads: int = 0
    denied: int = 0
    per_tool_calls: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "calls": self.calls,
                "writes": self.writes,
                "detail_reads": self.detail_reads,
                "denied": self.denied,
                "max_calls": self.max_calls,
                "max_writes": self.max_writes,
                "max_detail_reads": self.max_detail_reads,
                "max_list_items": self.max_list_items,
                "per_tool_calls": dict(sorted(self.per_tool_calls.items())),
            }


_ACTIVE_BUDGET: ContextVar[RuntimeToolBudget | None] = ContextVar(
    "hermes_runtime_tool_budget", default=None
)

_DETAIL_READ_TOOLS = frozenset({"kanban_show", "kanban_attachments", "runbook_get"})
_WRITE_TOOLS = frozenset(
    {
        "kanban_complete",
        "kanban_block",
        "kanban_request_review",
        "kanban_request_changes",
        "kanban_heartbeat",
        "kanban_comment",
        "kanban_archive_stale",
        "kanban_attach",
        "kanban_attach_url",
        "kanban_create",
        "kanban_unblock",
        "kanban_link",
        "workforce_signal",
        "workforce_goals:publish",
        "workforce_vision:request",
        "workforce_vision:respond",
        "workforce_handoff",
        "runbook_propose_create",
        "runbook_propose_edit",
    }
)


def activate_runtime_tool_budget(
    config: dict[str, Any] | None,
) -> tuple[Token, RuntimeToolBudget | None]:
    """Install a validated runtime budget and return its reset token/state."""
    if not config:
        return _ACTIVE_BUDGET.set(None), None
    if not isinstance(config, dict):
        raise ValueError("runtime_tool_budget must be a mapping")

    def positive_int(name: str) -> int:
        value = config.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"runtime_tool_budget.{name} must be a positive integer")
        return value

    raw_allowed = config.get("allowed_tools")
    if not isinstance(raw_allowed, list) or not raw_allowed:
        raise ValueError("runtime_tool_budget.allowed_tools must be a non-empty list")
    allowed = frozenset(str(item).strip() for item in raw_allowed if str(item).strip())
    if len(allowed) != len(raw_allowed):
        raise ValueError("runtime_tool_budget.allowed_tools must contain unique names")
    max_calls = positive_int("max_calls")
    max_writes = positive_int("max_writes")
    max_detail_reads = positive_int("max_detail_reads")
    max_list_items = positive_int("max_list_items")
    raw_write_tools = config.get("write_tools", [])
    if not isinstance(raw_write_tools, list):
        raise ValueError("runtime_tool_budget.write_tools must be a list")
    write_tools = frozenset(
        str(item).strip() for item in raw_write_tools if str(item).strip()
    )
    if len(write_tools) != len(raw_write_tools):
        raise ValueError(
            "runtime_tool_budget.write_tools must contain unique non-empty names"
        )
    if not write_tools <= allowed:
        raise ValueError(
            "runtime_tool_budget.write_tools must be a subset of allowed_tools"
        )
    raw_tool_limits = config.get("tool_call_limits", {})
    if not isinstance(raw_tool_limits, dict):
        raise ValueError("runtime_tool_budget.tool_call_limits must be a mapping")
    tool_call_limits: dict[str, int] = {}
    for raw_name, raw_limit in raw_tool_limits.items():
        name = str(raw_name).strip()
        if not name or name not in allowed:
            raise ValueError(
                "runtime_tool_budget.tool_call_limits keys must be allowed tools"
            )
        if name in tool_call_limits:
            raise ValueError(
                "runtime_tool_budget.tool_call_limits must contain unique "
                "non-empty names"
            )
        if isinstance(raw_limit, bool) or not isinstance(raw_limit, int) or raw_limit < 1:
            raise ValueError(
                "runtime_tool_budget.tool_call_limits values must be positive integers"
            )
        if raw_limit > max_calls:
            raise ValueError(
                "runtime_tool_budget.tool_call_limits values cannot exceed max_calls"
            )
        tool_call_limits[name] = raw_limit
    state = RuntimeToolBudget(
        max_calls=max_calls,
        max_writes=max_writes,
        max_detail_reads=max_detail_reads,
        max_list_items=max_list_items,
        allowed_tools=allowed,
        write_tools=write_tools,
        tool_call_limits=tool_call_limits,
    )
    return _ACTIVE_BUDGET.set(state), state


def reset_runtime_tool_budget(token: Token) -> None:
    _ACTIVE_BUDGET.reset(token)


def charge_runtime_tool_attempt(name: str) -> bool:
    """Charge one allowed invocation before tool-specific input preflight."""
    budget = _ACTIVE_BUDGET.get()
    if budget is None:
        return False
    with budget._lock:
        if name not in budget.allowed_tools:
            budget.denied += 1
            raise RuntimeToolBudgetError(f"tool {name!r} is not allowed for this bounded run")
        if budget.calls >= budget.max_calls:
            budget.denied += 1
            raise RuntimeToolBudgetError(
                f"tool-call budget exhausted ({budget.max_calls} calls)"
            )
        tool_calls = budget.per_tool_calls.get(name, 0)
        tool_limit = budget.tool_call_limits.get(name)
        if tool_limit is not None and tool_calls >= tool_limit:
            budget.denied += 1
            raise RuntimeToolBudgetError(
                f"tool {name!r} call budget exhausted ({tool_limit} calls)"
            )
        budget.calls += 1
        budget.per_tool_calls[name] = tool_calls + 1
    return True


def enforce_runtime_tool_budget(
    name: str,
    args: dict[str, Any],
    *,
    attempt_charged: bool = False,
) -> dict[str, Any]:
    """Reserve post-preflight sub-budgets and clamp bounded list arguments."""
    budget = _ACTIVE_BUDGET.get()
    if budget is None:
        return args
    if not attempt_charged:
        charge_runtime_tool_attempt(name)
    with budget._lock:
        action_name = f"{name}:{str(args.get('action') or '').strip()}"
        is_write = (
            name in _WRITE_TOOLS
            or action_name in _WRITE_TOOLS
            or name in budget.write_tools
        )
        is_detail = name in _DETAIL_READ_TOOLS
        if is_write and budget.writes >= budget.max_writes:
            budget.denied += 1
            raise RuntimeToolBudgetError(
                f"write budget exhausted ({budget.max_writes} write)"
            )
        if is_detail and budget.detail_reads >= budget.max_detail_reads:
            budget.denied += 1
            raise RuntimeToolBudgetError(
                f"detail-read budget exhausted ({budget.max_detail_reads} reads)"
            )
        if is_write:
            budget.writes += 1
        if is_detail:
            budget.detail_reads += 1

    bounded_args = dict(args)
    if name in {"kanban_list", "workforce_vision", "workforce_observe_buzz"}:
        requested = bounded_args.get("limit")
        try:
            requested_limit = int(requested) if requested is not None else budget.max_list_items
        except (TypeError, ValueError):
            requested_limit = budget.max_list_items
        bounded_args["limit"] = min(max(requested_limit, 1), budget.max_list_items)
    return bounded_args
