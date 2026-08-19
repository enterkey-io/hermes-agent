"""Typed records for the Hermes Workflow Registry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


WORKFLOW_STATUSES = {"draft", "active", "paused", "degraded", "retired"}
RUNTIME_KINDS = {"hermes", "script", "sim", "n8n", "external_cli"}
STEP_RUN_STATUSES = {
    "pending",
    "running",
    "succeeded",
    "failed",
    "skipped",
    "waiting_for_approval",
    "cancelled",
}
RUN_STATUSES = {
    "pending",
    "running",
    "succeeded",
    "failed",
    "cancelled",
}
TERMINAL_RUN_STATUSES = {"succeeded", "failed", "cancelled"}
TERMINAL_STEP_STATUSES = {"succeeded", "failed", "skipped", "cancelled"}


class WorkflowRegistryError(Exception):
    """Base class for workflow registry failures."""


class WorkflowNotFoundError(WorkflowRegistryError):
    """Raised when a workflow or run cannot be found."""


class WorkflowConflictError(WorkflowRegistryError):
    """Raised when optimistic concurrency or uniqueness checks fail."""


class WorkflowStateError(WorkflowRegistryError):
    """Raised when a state transition is invalid."""


@dataclass(frozen=True)
class WorkflowDefinition:
    id: str
    slug: str
    name: str
    description: str | None
    owner_profile: str
    workforce_managed: bool
    status: str
    runtime_kind: str
    runtime_ref: str | None
    source_path: str | None
    source_hash: str | None
    source_revision: str | None
    kanban_board: str | None
    repair_task_id: str | None
    dedupe_strategy: str | None
    timeout_seconds: int | None
    max_attempts: int | None
    version: int
    created_at: int
    updated_at: int
    retired_at: int | None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class WorkflowStep:
    id: str
    workflow_id: str
    step_key: str
    position: int
    name: str
    description: str | None
    executor_profile: str | None
    runtime_kind: str | None
    runtime_ref: str | None
    input_contract: dict[str, Any] | None
    output_contract: dict[str, Any] | None
    approval_policy: str | None
    timeout_seconds: int | None
    max_attempts: int | None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class WorkflowRun:
    id: str
    workflow_id: str
    trigger_kind: str
    trigger_ref: str | None
    dedupe_key: str | None
    status: str
    current_step_key: str | None
    started_at: int
    ended_at: int | None
    summary: str | None
    error: str | None
    kanban_task_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class WorkflowStepRun:
    id: str
    workflow_run_id: str
    step_key: str
    attempt: int
    status: str
    started_at: int | None
    ended_at: int | None
    summary: str | None
    error: str | None
    output_refs: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()
