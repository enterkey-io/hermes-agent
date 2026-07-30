"""Opaque, process-local execution capabilities issued by trusted runtimes.

These objects are control-plane state. They must never be serialized, copied,
or represented in model-visible tool arguments or results.
"""

from __future__ import annotations

import os
import re
import secrets
import threading
from dataclasses import dataclass
from typing import Any, Callable


_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_ISSUER = object()


class ExecutionCapabilityError(RuntimeError):
    """Raised when execution capability validation fails closed."""


class _NonTransferable:
    def __copy__(self):
        raise TypeError(f"{type(self).__name__} cannot be copied")

    def __deepcopy__(self, memo):
        raise TypeError(f"{type(self).__name__} cannot be copied")

    def __reduce__(self):
        raise TypeError(f"{type(self).__name__} cannot be serialized")

    def __reduce_ex__(self, protocol):
        raise TypeError(f"{type(self).__name__} cannot be serialized")


@dataclass(frozen=True, slots=True)
class CronJobCapabilityRequirement:
    """A plugin-declared requirement for one exact scheduler job."""

    job_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.job_id, str) or not _JOB_ID_RE.fullmatch(self.job_id):
            raise ValueError("cron job capability requires a valid exact job ID")


class _ExecutionState:
    __slots__ = (
        "active",
        "fingerprint",
        "job_id",
        "lock",
        "owner",
        "pid",
        "scoped_state",
    )

    def __init__(self, job_id: str) -> None:
        self.active = True
        self.fingerprint = secrets.token_hex(16)
        self.job_id = job_id
        self.lock = threading.RLock()
        self.owner: object | None = None
        self.pid = os.getpid()
        self.scoped_state: dict[str, dict[str, Any]] = {}

    def require_live(self) -> None:
        if not self.active or self.pid != os.getpid():
            raise ExecutionCapabilityError("execution capability is unavailable")


class CronJobExecutionContext(_NonTransferable):
    """Opaque context issued for one live scheduler execution."""

    __slots__ = ("__state",)

    def __init__(self, issuer: object, job_id: str) -> None:
        if issuer is not _ISSUER:
            raise ExecutionCapabilityError(
                "cron job execution contexts may only be scheduler-issued"
            )
        self.__state = _ExecutionState(job_id)

    def _state(self) -> _ExecutionState:
        return self.__state

    def __repr__(self) -> str:
        return "<CronJobExecutionContext opaque>"


class ToolInvocationGrant(_NonTransferable):
    """One-use proof that a bound agent initiated an exact tool call."""

    __slots__ = ("__consumed", "__state", "__tool_name")

    def __init__(
        self,
        issuer: object,
        state: _ExecutionState,
        tool_name: str,
    ) -> None:
        if issuer is not _ISSUER:
            raise ExecutionCapabilityError(
                "tool invocation grants may only be runtime-issued"
            )
        self.__consumed = False
        self.__state = state
        self.__tool_name = tool_name

    def _values(self) -> tuple[_ExecutionState, str, bool]:
        return self.__state, self.__tool_name, self.__consumed

    def _consume(self) -> None:
        self.__consumed = True

    def __repr__(self) -> str:
        return "<ToolInvocationGrant opaque>"


class ValidatedExecutionRuntime(_NonTransferable):
    """Minimal live runtime surface made available to a trusted tool handler."""

    __slots__ = ("__state",)

    def __init__(self, issuer: object, state: _ExecutionState) -> None:
        if issuer is not _ISSUER:
            raise ExecutionCapabilityError(
                "validated execution runtimes may only be runtime-issued"
            )
        self.__state = state

    def scoped_state(
        self,
        namespace: str,
        factory: Callable[[], dict[str, Any]] = dict,
    ) -> dict[str, Any]:
        if not isinstance(namespace, str) or not namespace.strip():
            raise ValueError("execution state namespace must be non-empty")
        state = self.__state
        with state.lock:
            state.require_live()
            if namespace not in state.scoped_state:
                value = factory()
                if not isinstance(value, dict):
                    raise TypeError("execution scoped-state factory must return a dict")
                state.scoped_state[namespace] = value
            return state.scoped_state[namespace]

    def __repr__(self) -> str:
        return "<ValidatedExecutionRuntime opaque>"


def cron_job_capability(job_id: str) -> CronJobCapabilityRequirement:
    return CronJobCapabilityRequirement(job_id=job_id)


def _issue_cron_job_execution_context(job_id: str) -> CronJobExecutionContext:
    requirement = cron_job_capability(job_id)
    return CronJobExecutionContext(_ISSUER, requirement.job_id)


def _bind_execution_context(
    context: CronJobExecutionContext,
    owner: object,
) -> None:
    state = _require_context(context)
    with state.lock:
        state.require_live()
        if state.owner is not None:
            raise ExecutionCapabilityError(
                "execution capability is already bound to an agent"
            )
        state.owner = owner


def _revoke_execution_context(context: CronJobExecutionContext) -> None:
    state = _require_context(context)
    with state.lock:
        state.active = False
        state.fingerprint = ""
        state.owner = None
        state.scoped_state.clear()


def execution_context_allows(
    context: object | None,
    requirement: CronJobCapabilityRequirement,
) -> bool:
    if not isinstance(context, CronJobExecutionContext):
        return False
    if not isinstance(requirement, CronJobCapabilityRequirement):
        return False
    state = context._state()
    with state.lock:
        return (
            state.active
            and state.pid == os.getpid()
            and state.owner is not None
            and state.job_id == requirement.job_id
        )


def execution_context_fingerprint(context: object | None) -> str:
    if not isinstance(context, CronJobExecutionContext):
        return ""
    state = context._state()
    with state.lock:
        if not state.active or state.pid != os.getpid() or state.owner is None:
            return ""
        return state.fingerprint


def _issue_tool_invocation_grant(
    context: CronJobExecutionContext,
    *,
    owner: object,
    tool_name: str,
) -> ToolInvocationGrant:
    if not isinstance(tool_name, str) or not tool_name:
        raise ValueError("tool name must be non-empty")
    state = _require_context(context)
    with state.lock:
        state.require_live()
        if state.owner is not owner:
            raise ExecutionCapabilityError(
                "execution capability is not bound to this agent"
            )
        return ToolInvocationGrant(_ISSUER, state, tool_name)


def _consume_tool_invocation_grant(
    grant: object | None,
    *,
    requirement: CronJobCapabilityRequirement,
    tool_name: str,
) -> ValidatedExecutionRuntime:
    if not isinstance(grant, ToolInvocationGrant):
        raise ExecutionCapabilityError("tool invocation capability is unavailable")
    if not isinstance(requirement, CronJobCapabilityRequirement):
        raise ExecutionCapabilityError("invalid tool execution requirement")

    state, granted_tool_name, consumed = grant._values()
    with state.lock:
        state.require_live()
        if consumed:
            raise ExecutionCapabilityError(
                "tool invocation capability has already been consumed"
            )
        if state.owner is None:
            raise ExecutionCapabilityError("execution capability is unbound")
        if granted_tool_name != tool_name:
            raise ExecutionCapabilityError(
                "tool invocation capability does not match this tool"
            )
        if state.job_id != requirement.job_id:
            raise ExecutionCapabilityError(
                "execution capability does not satisfy this tool"
            )
        grant._consume()
        return ValidatedExecutionRuntime(_ISSUER, state)


def _require_context(context: object) -> _ExecutionState:
    if not isinstance(context, CronJobExecutionContext):
        raise ExecutionCapabilityError("execution capability is unavailable")
    return context._state()
