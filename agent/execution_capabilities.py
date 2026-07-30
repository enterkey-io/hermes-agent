"""Opaque execution capabilities issued by the trusted cron dispatcher."""

from __future__ import annotations

import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:-]{0,127}$")
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


def _validated_identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{label} must be a valid exact identifier")
    return value


def _canonical_home(value: object) -> str:
    if not isinstance(value, (str, os.PathLike)):
        raise ValueError("Hermes home must be an absolute path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("Hermes home must be an absolute path")
    return str(path.resolve(strict=False))


def _validated_tool_name(value: object) -> str:
    if not isinstance(value, str) or not _TOOL_NAME_RE.fullmatch(value):
        raise ValueError("tool name must be a valid exact identifier")
    return value


@dataclass(frozen=True, slots=True)
class CronJobCapabilityRequirement:
    """Plugin-owned trust configuration for one profile and cron job."""

    profile_name: str
    hermes_home: str
    job_id: str
    registration_owner: str = "host"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "profile_name",
            _validated_identifier(self.profile_name, "profile name"),
        )
        object.__setattr__(self, "hermes_home", _canonical_home(self.hermes_home))
        object.__setattr__(
            self,
            "job_id",
            _validated_identifier(self.job_id, "cron job ID"),
        )
        object.__setattr__(
            self,
            "registration_owner",
            _validated_identifier(
                self.registration_owner,
                "capability registration owner",
            ),
        )


class TrustedCronDispatch(_NonTransferable):
    """One-use dispatcher permit bound to one selected job object."""

    __slots__ = (
        "__allowed_tools",
        "__consumed",
        "__execution_id",
        "__handler_timeout",
        "__hermes_home",
        "__job",
        "__job_id",
        "__lock",
        "__pid",
        "__profile_name",
        "__registration_owner",
    )

    def __init__(
        self,
        issuer: object,
        *,
        job: dict,
        profile_name: str,
        hermes_home: str,
        execution_id: str,
        allowed_tools: frozenset[str],
        handler_timeout: float,
        registration_owner: str,
    ) -> None:
        if issuer is not _ISSUER:
            raise ExecutionCapabilityError(
                "trusted cron dispatch permits are host-issued"
            )
        self.__allowed_tools = allowed_tools
        self.__consumed = False
        self.__execution_id = execution_id
        self.__handler_timeout = handler_timeout
        self.__hermes_home = hermes_home
        self.__job = job
        self.__job_id = str(job["id"])
        self.__lock = threading.Lock()
        self.__pid = os.getpid()
        self.__profile_name = profile_name
        self.__registration_owner = registration_owner

    def _consume(
        self,
        *,
        job: dict,
        execution_id: str,
        profile_name: str,
        hermes_home: str,
        registration_owner: str,
    ) -> tuple:
        with self.__lock:
            if self.__pid != os.getpid():
                raise ExecutionCapabilityError(
                    "trusted cron dispatch cannot cross processes"
                )
            if self.__consumed:
                raise ExecutionCapabilityError(
                    "trusted cron dispatch has already been consumed"
                )
            if self.__job is not job:
                raise ExecutionCapabilityError(
                    "trusted cron dispatch does not match this job object"
                )
            if str(job.get("id") or "") != self.__job_id:
                raise ExecutionCapabilityError(
                    "trusted cron dispatch does not match this job ID"
                )
            if (
                self.__profile_name != profile_name
                or self.__hermes_home != hermes_home
            ):
                raise ExecutionCapabilityError(
                    "trusted cron dispatch does not match this profile"
                )
            if self.__execution_id != execution_id:
                raise ExecutionCapabilityError(
                    "trusted cron dispatch does not match this execution"
                )
            if self.__registration_owner != registration_owner:
                raise ExecutionCapabilityError(
                    "trusted cron dispatch does not match this capability owner"
                )
            self.__consumed = True
            return (
                self.__profile_name,
                self.__hermes_home,
                self.__job_id,
                self.__execution_id,
                self.__allowed_tools,
                self.__handler_timeout,
                self.__registration_owner,
            )

    def __repr__(self) -> str:
        return "<TrustedCronDispatch opaque>"


class _ExecutionState:
    __slots__ = (
        "active",
        "active_invocations",
        "allowed_tools",
        "closing",
        "condition",
        "execution_id",
        "fingerprint",
        "handler_timeout",
        "hermes_home",
        "job_id",
        "lock",
        "owner",
        "pid",
        "profile_name",
        "registration_owner",
        "scoped_state",
        "uncertain_operations",
    )

    def __init__(
        self,
        *,
        profile_name: str,
        hermes_home: str,
        job_id: str,
        execution_id: str,
        allowed_tools: frozenset[str],
        handler_timeout: float,
        registration_owner: str,
    ) -> None:
        self.active = True
        self.active_invocations: dict[str, dict[str, Any]] = {}
        self.allowed_tools = allowed_tools
        self.closing = False
        self.execution_id = execution_id
        self.fingerprint = secrets.token_hex(16)
        self.handler_timeout = handler_timeout
        self.hermes_home = hermes_home
        self.job_id = job_id
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.owner: object | None = None
        self.pid = os.getpid()
        self.profile_name = profile_name
        self.registration_owner = registration_owner
        self.scoped_state: dict[str, dict[str, Any]] = {}
        self.uncertain_operations: set[str] = set()

    def require_live(self) -> None:
        if not self.active or self.closing or self.pid != os.getpid():
            raise ExecutionCapabilityError("execution capability is unavailable")


class CronJobExecutionContext(_NonTransferable):
    """Opaque context for one trusted scheduler execution."""

    __slots__ = ("__state",)

    def __init__(self, issuer: object, state: _ExecutionState) -> None:
        if issuer is not _ISSUER:
            raise ExecutionCapabilityError(
                "cron job execution contexts are host-issued"
            )
        self.__state = state

    def _state(self) -> _ExecutionState:
        return self.__state

    def __repr__(self) -> str:
        return "<CronJobExecutionContext opaque>"


class ToolInvocationGrant(_NonTransferable):
    """One-use proof for one exact agent, context, and tool."""

    __slots__ = ("__consumed", "__context", "__owner", "__state", "__tool_name")

    def __init__(
        self,
        issuer: object,
        *,
        context: CronJobExecutionContext,
        state: _ExecutionState,
        owner: object,
        tool_name: str,
    ) -> None:
        if issuer is not _ISSUER:
            raise ExecutionCapabilityError(
                "tool invocation grants are host-issued"
            )
        self.__consumed = False
        self.__context = context
        self.__owner = owner
        self.__state = state
        self.__tool_name = tool_name

    def _consume_locked(
        self,
        *,
        state: _ExecutionState,
        context: CronJobExecutionContext,
        owner: object,
        tool_name: str,
    ) -> None:
        if self.__consumed:
            raise ExecutionCapabilityError(
                "tool invocation capability has already been consumed"
            )
        if (
            self.__state is not state
            or self.__context is not context
            or self.__owner is not owner
            or self.__tool_name != tool_name
        ):
            raise ExecutionCapabilityError(
                "tool invocation capability does not match this invocation"
            )
        self.__consumed = True

    def __repr__(self) -> str:
        return "<ToolInvocationGrant opaque>"


class ValidatedExecutionRuntime(_NonTransferable):
    """Deadline-aware runtime passed only to a validated protected handler."""

    __slots__ = ("__deadline", "__invocation_id", "__state")

    def __init__(
        self,
        issuer: object,
        *,
        state: _ExecutionState,
        invocation_id: str,
        deadline: float,
    ) -> None:
        if issuer is not _ISSUER:
            raise ExecutionCapabilityError(
                "validated execution runtimes are host-issued"
            )
        self.__deadline = deadline
        self.__invocation_id = invocation_id
        self.__state = state

    def remaining_seconds(self) -> float:
        with self.__state.lock:
            self.__state.require_live()
            remaining = self.__deadline - time.monotonic()
            if remaining <= 0:
                raise ExecutionCapabilityError(
                    "protected tool execution deadline expired"
                )
            return remaining

    def check_active(self, *, minimum_remaining_seconds: float = 0.0) -> None:
        if minimum_remaining_seconds < 0:
            raise ValueError("minimum remaining time cannot be negative")
        if self.remaining_seconds() <= minimum_remaining_seconds:
            raise ExecutionCapabilityError(
                "insufficient protected execution time remains"
            )

    def bounded_timeout(
        self,
        maximum_seconds: float,
        *,
        reserve_seconds: float = 0.05,
    ) -> float:
        if maximum_seconds <= 0 or reserve_seconds < 0:
            raise ValueError("protected request timeout bounds are invalid")
        remaining = self.remaining_seconds() - reserve_seconds
        if remaining <= 0:
            raise ExecutionCapabilityError(
                "insufficient protected execution time remains"
            )
        return min(float(maximum_seconds), remaining)

    def mark_external_mutation_started(self, reconciliation_key: str) -> None:
        key = _validated_identifier(reconciliation_key, "reconciliation key")
        self.check_active(minimum_remaining_seconds=0.0)
        with self.__state.lock:
            record = self.__state.active_invocations.get(self.__invocation_id)
            if record is None:
                raise ExecutionCapabilityError(
                    "protected invocation is no longer active"
                )
            record["mutation_started"] = True
            record["reconciliation_key"] = key

    def mark_external_mutation_resolved(self) -> None:
        with self.__state.lock:
            record = self.__state.active_invocations.get(self.__invocation_id)
            if record is None or not record.get("mutation_started"):
                raise ExecutionCapabilityError(
                    "no external mutation is awaiting resolution"
                )
            record["mutation_resolved"] = True

    def scoped_state(
        self,
        namespace: str,
        factory: Callable[[], dict[str, Any]] = dict,
    ) -> dict[str, Any]:
        if not isinstance(namespace, str) or not namespace.strip():
            raise ValueError("execution state namespace must be non-empty")
        self.check_active()
        state = self.__state
        with state.lock:
            if namespace not in state.scoped_state:
                value = factory()
                if not isinstance(value, dict):
                    raise TypeError(
                        "execution scoped-state factory must return a dict"
                    )
                state.scoped_state[namespace] = value
            return state.scoped_state[namespace]

    def _settle(self) -> bool:
        state = self.__state
        with state.condition:
            record = state.active_invocations.pop(self.__invocation_id, None)
            uncertain = bool(
                record
                and record.get("mutation_started")
                and not record.get("mutation_resolved")
            )
            if uncertain:
                state.uncertain_operations.add(
                    str(record.get("reconciliation_key") or "unknown")
                )
                state.closing = True
                state.active = False
                state.fingerprint = ""
            state.condition.notify_all()
            return uncertain

    def __repr__(self) -> str:
        return "<ValidatedExecutionRuntime opaque>"


@dataclass(frozen=True, slots=True)
class ExecutionSettlement:
    settled: bool
    reconciliation_required: bool
    active_invocations: int
    uncertain_operations: tuple[str, ...]


def cron_job_capability(
    *,
    profile_name: str,
    hermes_home: str | os.PathLike[str],
    job_id: str,
    registration_owner: str = "host",
) -> CronJobCapabilityRequirement:
    return CronJobCapabilityRequirement(
        profile_name=profile_name,
        hermes_home=str(hermes_home),
        job_id=job_id,
        registration_owner=registration_owner,
    )


def _issue_trusted_cron_dispatch(
    *,
    job: dict,
    profile_name: str,
    hermes_home: str | os.PathLike[str],
    execution_id: str,
    allowed_tools: set[str] | frozenset[str],
    protected_handler_timeout_seconds: float,
    registration_owner: str = "host",
) -> TrustedCronDispatch:
    if not isinstance(job, dict):
        raise ValueError("trusted cron dispatch requires a job object")
    job_id = _validated_identifier(job.get("id"), "cron job ID")
    profile = _validated_identifier(profile_name, "profile name")
    home = _canonical_home(hermes_home)
    execution = _validated_identifier(execution_id, "execution ID")
    owner = _validated_identifier(
        registration_owner,
        "capability registration owner",
    )
    if not isinstance(allowed_tools, (set, frozenset)) or not allowed_tools:
        raise ValueError("trusted cron dispatch requires an explicit tool allowlist")
    tools = frozenset(
        _validated_tool_name(name)
        for name in allowed_tools
    )
    timeout = float(protected_handler_timeout_seconds)
    if not 0.05 <= timeout <= 60.0:
        raise ValueError("protected handler timeout must be between 0.05 and 60s")
    return TrustedCronDispatch(
        _ISSUER,
        job=job,
        profile_name=profile,
        hermes_home=home,
        execution_id=execution,
        allowed_tools=tools,
        handler_timeout=timeout,
        registration_owner=owner,
    )


def _issue_cron_job_execution_context(
    *,
    permit: TrustedCronDispatch,
    job: dict,
    execution_id: str,
    profile_name: str,
    hermes_home: str | os.PathLike[str],
    requirement: CronJobCapabilityRequirement | None = None,
) -> CronJobExecutionContext:
    if not isinstance(permit, TrustedCronDispatch):
        raise ExecutionCapabilityError("trusted cron dispatch is unavailable")
    profile = _validated_identifier(profile_name, "profile name")
    home = _canonical_home(hermes_home)
    if requirement is None:
        requirement = cron_job_capability(
            profile_name=profile,
            hermes_home=home,
            job_id=str(job.get("id") or ""),
        )
    if not isinstance(requirement, CronJobCapabilityRequirement):
        raise ExecutionCapabilityError("invalid cron capability requirement")
    values = permit._consume(
        job=job,
        execution_id=execution_id,
        profile_name=profile,
        hermes_home=home,
        registration_owner=requirement.registration_owner,
    )
    (
        profile_name,
        home,
        job_id,
        execution,
        allowed_tools,
        timeout,
        registration_owner,
    ) = values
    if (
        profile_name != requirement.profile_name
        or home != requirement.hermes_home
        or job_id != requirement.job_id
    ):
        raise ExecutionCapabilityError(
            "trusted cron dispatch does not satisfy this job requirement"
        )
    state = _ExecutionState(
        profile_name=profile_name,
        hermes_home=home,
        job_id=job_id,
        execution_id=execution,
        allowed_tools=allowed_tools,
        handler_timeout=timeout,
        registration_owner=registration_owner,
    )
    return CronJobExecutionContext(_ISSUER, state)


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


def _execution_context_matches_unbound_job(
    context: object,
    *,
    requirement: CronJobCapabilityRequirement,
    execution_id: str,
) -> bool:
    """Validate an internal pre-admission before the agent claims ownership."""
    if not isinstance(context, CronJobExecutionContext):
        return False
    state = context._state()
    with state.lock:
        return (
            state.active
            and not state.closing
            and state.pid == os.getpid()
            and state.owner is None
            and state.profile_name == requirement.profile_name
            and state.hermes_home == requirement.hermes_home
            and state.job_id == requirement.job_id
            and state.registration_owner == requirement.registration_owner
            and state.execution_id == execution_id
        )


def _finalize_execution_context(
    context: CronJobExecutionContext,
    *,
    timeout_seconds: float,
) -> ExecutionSettlement:
    state = _require_context(context)
    timeout = max(0.0, float(timeout_seconds))
    deadline = time.monotonic() + timeout
    with state.condition:
        state.closing = True
        while state.active_invocations:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            state.condition.wait(timeout=remaining)
        active = len(state.active_invocations)
        if active:
            for record in state.active_invocations.values():
                state.uncertain_operations.add(
                    str(record.get("reconciliation_key") or "in-flight")
                )
        state.active = False
        state.fingerprint = ""
        if not state.uncertain_operations:
            state.owner = None
        return ExecutionSettlement(
            settled=active == 0,
            reconciliation_required=bool(state.uncertain_operations),
            active_invocations=active,
            uncertain_operations=tuple(sorted(state.uncertain_operations)),
        )


def _revoke_execution_context(context: CronJobExecutionContext) -> None:
    _finalize_execution_context(context, timeout_seconds=0.0)


def execution_context_allows(
    context: object | None,
    requirement: CronJobCapabilityRequirement,
    *,
    owner: object,
) -> bool:
    if not isinstance(context, CronJobExecutionContext):
        return False
    if not isinstance(requirement, CronJobCapabilityRequirement):
        return False
    state = context._state()
    with state.lock:
        return (
            state.active
            and not state.closing
            and state.pid == os.getpid()
            and state.owner is owner
            and state.profile_name == requirement.profile_name
            and state.hermes_home == requirement.hermes_home
            and state.job_id == requirement.job_id
            and state.registration_owner == requirement.registration_owner
        )


def execution_context_fingerprint(
    context: object | None,
    *,
    owner: object,
) -> str:
    if not isinstance(context, CronJobExecutionContext):
        return ""
    state = context._state()
    with state.lock:
        if (
            not state.active
            or state.closing
            or state.pid != os.getpid()
            or state.owner is not owner
        ):
            return ""
        return state.fingerprint


def execution_context_requires_reconciliation(
    context: object | None,
    *,
    owner: object,
) -> bool:
    """Return whether this exact owner has a terminal uncertain mutation."""
    if not isinstance(context, CronJobExecutionContext):
        return False
    state = context._state()
    with state.lock:
        return (
            state.pid == os.getpid()
            and state.owner is owner
            and bool(state.uncertain_operations)
        )


def _issue_tool_invocation_grant(
    context: CronJobExecutionContext,
    *,
    owner: object,
    tool_name: str,
) -> ToolInvocationGrant:
    name = _validated_tool_name(tool_name)
    state = _require_context(context)
    with state.lock:
        state.require_live()
        if state.owner is not owner:
            raise ExecutionCapabilityError(
                "execution capability is not bound to this agent"
            )
        if name not in state.allowed_tools:
            raise ExecutionCapabilityError(
                "tool is outside this execution allowlist"
            )
        return ToolInvocationGrant(
            _ISSUER,
            context=context,
            state=state,
            owner=owner,
            tool_name=name,
        )


def _consume_tool_invocation_grant(
    grant: object | None,
    *,
    requirement: CronJobCapabilityRequirement,
    tool_name: str,
    owner: object,
    execution_context: CronJobExecutionContext,
) -> ValidatedExecutionRuntime:
    if not isinstance(grant, ToolInvocationGrant):
        raise ExecutionCapabilityError("tool invocation capability is unavailable")
    if not isinstance(requirement, CronJobCapabilityRequirement):
        raise ExecutionCapabilityError("invalid tool execution requirement")
    if not isinstance(execution_context, CronJobExecutionContext):
        raise ExecutionCapabilityError("execution context is unavailable")

    state = execution_context._state()
    name = _validated_tool_name(tool_name)
    with state.lock:
        state.require_live()
        if state.owner is not owner:
            raise ExecutionCapabilityError(
                "execution capability is not bound to this agent"
            )
        if (
            state.profile_name != requirement.profile_name
            or state.hermes_home != requirement.hermes_home
            or state.job_id != requirement.job_id
            or state.registration_owner != requirement.registration_owner
            or name not in state.allowed_tools
        ):
            raise ExecutionCapabilityError(
                "execution capability does not satisfy this tool"
            )
        grant._consume_locked(
            state=state,
            context=execution_context,
            owner=owner,
            tool_name=name,
        )
        invocation_id = secrets.token_hex(16)
        deadline = time.monotonic() + state.handler_timeout
        state.active_invocations[invocation_id] = {
            "tool_name": name,
            "mutation_started": False,
            "mutation_resolved": False,
            "reconciliation_key": f"invocation:{invocation_id}",
        }
        return ValidatedExecutionRuntime(
            _ISSUER,
            state=state,
            invocation_id=invocation_id,
            deadline=deadline,
        )


def _require_context(context: object) -> _ExecutionState:
    if not isinstance(context, CronJobExecutionContext):
        raise ExecutionCapabilityError("execution capability is unavailable")
    return context._state()
