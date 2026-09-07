"""Canonical organization loading and policy for proactive workforces."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from hermes_constants import get_default_hermes_root, get_hermes_home


ALLOWED_STATUSES = {"active", "planned", "friend", "retired", "artifact"}
ALLOWED_DEPARTMENTS = {
    "Product", "Agent Systems", "Operations", "Marketing", "Trading",
    "Finance", "Vision",
}
EXECUTABLE_STATUSES = {"active", "planned"}

# Same-card Kanban phases.  Keep this tuple ordered: generated role contracts
# use it for stable, reviewable output while validators use the companion set.
LIFECYCLE_PHASES = (
    "execution",
    "technical_review",
    "intent_review",
    "activation",
    "live_acceptance",
    "closure",
    "recovery",
)
LIFECYCLE_PHASE_SET = frozenset(LIFECYCLE_PHASES)


class WorkforceOrganizationError(ValueError):
    """Raised when canonical workforce metadata is invalid."""


class WorkforceOrganizationAbsentError(WorkforceOrganizationError):
    """Raised when no workforce organization file exists."""


@dataclass(frozen=True)
class WorkforceAgent:
    agent: str
    display_name: str
    status: str
    operational: bool
    department: str | None
    function: str | None
    manager: str | None
    direct_reports: tuple[str, ...]
    mission: str
    owned_outcomes: tuple[str, ...]
    authority: tuple[str, ...]
    prohibited_actions: tuple[str, ...]
    escalation_target: str | None
    cross_team_request_path: str | None
    buzz_rooms: tuple[str, ...]
    profile_path: str | None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "WorkforceAgent":
        def text(key: str, *, required: bool = False) -> str | None:
            value = raw.get(key)
            if value is None:
                if required:
                    raise WorkforceOrganizationError(f"agent missing {key}")
                return None
            if not isinstance(value, str) or (required and not value.strip()):
                raise WorkforceOrganizationError(f"agent {key} must be a string")
            return value.strip() or None

        def strings(key: str) -> tuple[str, ...]:
            value = raw.get(key, [])
            if not isinstance(value, list) or not all(
                isinstance(item, str) and item.strip() for item in value
            ):
                raise WorkforceOrganizationError(f"agent {key} must be a list of strings")
            return tuple(item.strip() for item in value)

        operational = raw.get("operational")
        if not isinstance(operational, bool):
            raise WorkforceOrganizationError("agent operational must be boolean")
        return cls(
            agent=text("agent", required=True) or "",
            display_name=text("display_name", required=True) or "",
            status=text("status", required=True) or "",
            operational=operational,
            department=text("department"),
            function=text("function"),
            manager=text("manager"),
            direct_reports=strings("direct_reports"),
            mission=text("mission", required=True) or "",
            owned_outcomes=strings("owned_outcomes"),
            authority=strings("authority"),
            prohibited_actions=strings("prohibited_actions"),
            escalation_target=text("escalation_target"),
            cross_team_request_path=text("cross_team_request_path"),
            buzz_rooms=strings("buzz_rooms"),
            profile_path=text("profile_path"),
        )

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        for key in ("direct_reports", "owned_outcomes", "authority", "prohibited_actions", "buzz_rooms"):
            data[key] = list(data[key])
        return data


@dataclass(frozen=True)
class WorkforceLifecycleRoute:
    """Compact lifecycle policy derived from one canonical org record.

    Receivers such as ``intent_validator`` and ``implementer`` deliberately
    name task-role slots rather than individual people: the card is the durable
    authority for those roles, while the organization supplies stable manager,
    QA, and activation ownership.
    """

    agent: str
    profile: str
    manager: str
    escalation_target: str
    accepted_work_classes: tuple[str, ...]
    accepted_phases: tuple[str, ...]
    normal_receiver: str
    failure_receiver: str
    technical_reviewer: str
    local_activation_owner: str
    external_activation_owner: str
    stuck_route: str


@dataclass(frozen=True)
class WorkforceOrganization:
    schema_version: int
    agents: Mapping[str, WorkforceAgent]
    source_path: Path
    technical_ownership: Mapping[str, str]

    def get(self, agent: str) -> WorkforceAgent:
        key = normalize_agent_id(agent)
        try:
            return self.agents[key]
        except KeyError as exc:
            raise WorkforceOrganizationError(f"unknown workforce agent: {agent}") from exc

    def resolve_profile(self, agent_or_profile: str) -> WorkforceAgent:
        """Resolve either a canonical agent id or a profile-directory name."""
        key = normalize_agent_id(agent_or_profile)
        if key in self.agents:
            return self.agents[key]
        matches = [
            item
            for item in self.agents.values()
            if item.profile_path
            and Path(item.profile_path).name.casefold() == key
        ]
        if len(matches) != 1:
            raise WorkforceOrganizationError(
                f"unknown or ambiguous workforce profile: {agent_or_profile}"
            )
        return matches[0]

    def from_profile_path(self, profile: str | Path) -> WorkforceAgent:
        value = Path(profile).name.casefold()
        matches = [
            item for item in self.agents.values()
            if item.profile_path and Path(item.profile_path).name.casefold() == value
        ]
        if len(matches) != 1:
            raise WorkforceOrganizationError(
                f"profile {value!r} resolves to {len(matches)} workforce agents"
            )
        return matches[0]

    def operational_agents(self, *, include_planned: bool = True) -> tuple[WorkforceAgent, ...]:
        statuses = EXECUTABLE_STATUSES if include_planned else {"active"}
        return tuple(
            item for item in self.agents.values()
            if item.operational and item.status in statuses
        )

    def validate_execution_profile(self, agent: str) -> WorkforceAgent:
        item = self.resolve_profile(agent)
        if not item.operational or item.status not in EXECUTABLE_STATUSES:
            raise WorkforceOrganizationError(
                f"{item.agent} is {item.status} and cannot own or execute active work"
            )
        return item

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "technical_ownership": dict(self.technical_ownership),
            "agents": [item.to_dict() for item in self.agents.values()],
        }


def normalize_agent_id(value: str) -> str:
    return str(value or "").strip().casefold()


def is_workforce_managed(metadata: Mapping[str, Any]) -> bool:
    """Return whether a workflow/runbook explicitly opts into workforce policy."""
    if metadata.get("workforce_managed") is True:
        return True
    related = metadata.get("related")
    return isinstance(related, Mapping) and related.get("workforce_managed") is True


def organization_path() -> Path:
    override = os.environ.get("HERMES_WORKFORCE_ORG", "").strip()
    return Path(override).expanduser() if override else get_default_hermes_root() / "organization" / "organization.yaml"


def load_organization(
    path: Path | None = None,
    *,
    validate_profiles: bool = False,
    source_text: str | None = None,
) -> WorkforceOrganization:
    source = (path or organization_path()).expanduser()
    try:
        raw = yaml.safe_load(
            source.read_text(encoding="utf-8-sig") if source_text is None else source_text
        ) or {}
    except FileNotFoundError as exc:
        raise WorkforceOrganizationAbsentError(
            f"organization file does not exist: {source}"
        ) from exc
    except OSError as exc:
        raise WorkforceOrganizationError(f"cannot read organization file {source}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise WorkforceOrganizationError(f"invalid organization YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise WorkforceOrganizationError("organization root must be a mapping")
    version = raw.get("schema_version")
    if version != 1:
        raise WorkforceOrganizationError(f"unsupported organization schema_version: {version!r}")
    rows = raw.get("agents")
    if not isinstance(rows, list) or not rows:
        raise WorkforceOrganizationError("organization agents must be a non-empty list")
    agents: dict[str, WorkforceAgent] = {}
    for raw_agent in rows:
        if not isinstance(raw_agent, dict):
            raise WorkforceOrganizationError("each organization agent must be a mapping")
        item = WorkforceAgent.from_mapping(raw_agent)
        key = normalize_agent_id(item.agent)
        if key != item.agent:
            raise WorkforceOrganizationError(f"agent id must be normalized lowercase: {item.agent!r}")
        if key in agents:
            raise WorkforceOrganizationError(f"duplicate agent id: {key}")
        agents[key] = item
    technical_ownership = raw.get("technical_ownership") or {}
    if not isinstance(technical_ownership, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in technical_ownership.items()
    ):
        raise WorkforceOrganizationError("technical_ownership must be a string mapping")
    org = WorkforceOrganization(version, agents, source, technical_ownership)
    validate_organization(org, validate_profiles=validate_profiles)
    return org


def active_workforce_agent() -> WorkforceAgent:
    """Resolve the current task-scoped Hermes profile to its workforce agent.

    ``get_hermes_home`` honors the context-local profile override used by the
    multiplexed gateway before it consults the process environment. Reading
    ``HERMES_HOME`` directly here can therefore attribute one conversation to
    the gateway process's launch profile instead of the active conversation.
    """
    home = get_hermes_home().expanduser()
    if home.parent.name != "profiles" or not home.name:
        raise WorkforceOrganizationError(
            "workforce tools require an active named profile"
        )
    return load_organization().from_profile_path(home)


def validate_organization(org: WorkforceOrganization, *, validate_profiles: bool = False) -> None:
    errors: list[str] = []
    for item in org.agents.values():
        if item.status not in ALLOWED_STATUSES:
            errors.append(f"{item.agent}: invalid status {item.status!r}")
        if item.department is not None and item.department not in ALLOWED_DEPARTMENTS:
            errors.append(f"{item.agent}: invalid department {item.department!r}")
        if item.operational and item.status not in EXECUTABLE_STATUSES:
            errors.append(f"{item.agent}: operational agents must be active or planned")
        if not item.operational and item.direct_reports and item.agent != "elliott":
            errors.append(f"{item.agent}: non-operational agent cannot have direct reports")
        if item.manager:
            manager = org.agents.get(normalize_agent_id(item.manager))
            if manager is None:
                errors.append(f"{item.agent}: unknown manager {item.manager!r}")
            elif item.agent not in manager.direct_reports:
                errors.append(f"{item.agent}: manager {manager.agent} does not list reciprocal report")
        elif item.operational:
            errors.append(f"{item.agent}: operational agent must have exactly one manager")
        for report_id in item.direct_reports:
            report = org.agents.get(normalize_agent_id(report_id))
            if report is None:
                errors.append(f"{item.agent}: unknown direct report {report_id!r}")
            elif report.manager != item.agent:
                errors.append(f"{item.agent}: direct report {report.agent} names manager {report.manager!r}")
        if validate_profiles and item.status == "active" and item.operational:
            if not item.profile_path or not Path(item.profile_path).is_dir():
                errors.append(f"{item.agent}: active profile path is missing")

    for start in org.agents:
        seen: list[str] = []
        current: str | None = start
        while current:
            if current in seen:
                cycle = " -> ".join([*seen[seen.index(current):], current])
                errors.append(f"manager cycle: {cycle}")
                break
            seen.append(current)
            node = org.agents.get(current)
            current = normalize_agent_id(node.manager) if node and node.manager else None
    for responsibility, owner in org.technical_ownership.items():
        if owner == "department_director":
            continue
        if normalize_agent_id(owner) not in org.agents:
            errors.append(f"technical ownership {responsibility}: unknown owner {owner!r}")
    if errors:
        raise WorkforceOrganizationError("; ".join(dict.fromkeys(errors)))


def validate_workflow_profiles(
    org: WorkforceOrganization, owner_profile: str, executor_profiles: Iterable[str | None]
) -> None:
    org.validate_execution_profile(owner_profile)
    for profile in executor_profiles:
        if profile:
            org.validate_execution_profile(profile)


def _technical_owner(org: WorkforceOrganization, key: str) -> str:
    owner = normalize_agent_id(org.technical_ownership.get(key, ""))
    if not owner or owner == "department_director":
        raise WorkforceOrganizationError(
            f"technical ownership {key!r} must resolve to one operational agent"
        )
    return org.validate_execution_profile(owner).agent


def derive_lifecycle_route(
    org: WorkforceOrganization,
    agent_or_profile: str,
) -> WorkforceLifecycleRoute:
    """Derive one operational agent's Kanban route from org + ownership.

    The derivation intentionally operates on role families (QA owner,
    activation owners, managers/directors, and specialists) instead of a
    hand-maintained table of every profile.  Adding another specialist under a
    department director therefore inherits the right return/stuck route.
    """
    agent = org.validate_execution_profile(agent_or_profile)
    profile = Path(agent.profile_path or "").name
    if not profile:
        raise WorkforceOrganizationError(
            f"{agent.agent} has no profile path for lifecycle dispatch"
        )

    qa_owner = _technical_owner(org, "qa")
    implementation_owner = _technical_owner(org, "implementation")
    local_owner = _technical_owner(org, "local_host_install_service_activation")
    external_owner = _technical_owner(org, "external_cloud_server_app_operations")

    manager = normalize_agent_id(agent.manager or agent.escalation_target or "")
    if not manager:
        raise WorkforceOrganizationError(
            f"{agent.agent} has no manager/escalation route"
        )
    # Elliott is retained authority, not an operational worker.  The org's
    # explicit escalation target remains visible, but routine internal stuck
    # work must stay inside the operating workforce.  Aurora and Grace are the
    # two top-level operational managers for their respective branches.
    escalation = normalize_agent_id(agent.escalation_target or manager)
    internal_manager = manager
    if internal_manager == "elliott":
        internal_manager = "aurora" if agent.agent == "aurora" else "grace"

    is_manager = bool(agent.direct_reports) or (agent.function or "").casefold() == "director"
    if agent.agent == qa_owner:
        phases = ("technical_review", "recovery")
        normal_receiver = "intent_validator"
    elif agent.agent in {local_owner, external_owner}:
        phases = ("execution", "activation", "recovery")
        normal_receiver = "original_author"
    elif is_manager or agent.agent in {"aurora", "grace"}:
        phases = (
            "execution",
            "intent_review",
            "live_acceptance",
            "closure",
            "recovery",
        )
        normal_receiver = "original_author"
    elif agent.agent == implementation_owner or "developer" in (agent.function or "").casefold():
        phases = ("execution", "recovery")
        normal_receiver = qa_owner
    else:
        phases = ("execution", "recovery")
        normal_receiver = internal_manager

    # Owned outcomes are the organization-native work classes.  Function and
    # department are included as compact fallback labels for newly added roles
    # that have not accumulated detailed outcome metadata yet.
    work_classes = tuple(agent.owned_outcomes)
    if not work_classes:
        fallback = agent.department or agent.function or "assigned work"
        work_classes = (fallback,)

    return WorkforceLifecycleRoute(
        agent=agent.agent,
        profile=profile,
        manager=manager,
        escalation_target=escalation,
        accepted_work_classes=work_classes,
        accepted_phases=phases,
        normal_receiver=normal_receiver,
        failure_receiver="implementer",
        technical_reviewer=qa_owner,
        local_activation_owner=local_owner,
        external_activation_owner=external_owner,
        stuck_route=internal_manager,
    )


_PHASE_ROLE = {
    "execution": "implementer",
    "technical_review": "technical_reviewer",
    "intent_review": "intent_validator",
    "activation": "activation_owner",
    "live_acceptance": "closure_owner",
    "closure": "closure_owner",
}


def validate_lifecycle_assignment(
    org: WorkforceOrganization,
    assignee: str,
    phase: str,
    roles: Mapping[str, Any],
) -> WorkforceAgent:
    """Validate that an operational assignee owns ``phase`` on this card."""
    normalized_phase = str(phase or "").strip().casefold()
    if normalized_phase not in LIFECYCLE_PHASE_SET:
        raise WorkforceOrganizationError(
            f"invalid lifecycle phase {phase!r}; expected one of {list(LIFECYCLE_PHASES)}"
        )
    agent = org.validate_execution_profile(assignee)
    route = derive_lifecycle_route(org, agent.agent)

    role_key = _PHASE_ROLE.get(normalized_phase)
    if role_key:
        expected = normalize_agent_id(roles.get(role_key, ""))
        if not expected:
            raise WorkforceOrganizationError(
                f"phase {normalized_phase} requires recorded {role_key}"
            )
        expected_agent = org.resolve_profile(expected).agent
        if agent.agent != expected_agent:
            raise WorkforceOrganizationError(
                f"phase {normalized_phase} belongs to {role_key} "
                f"{expected_agent!r}, not {agent.agent!r}"
            )
    elif normalized_phase == "recovery":
        allowed = {
            normalize_agent_id(roles.get("return_to", "")),
            route.stuck_route,
            route.manager,
            route.escalation_target,
        }
        if agent.agent not in {value for value in allowed if value}:
            raise WorkforceOrganizationError(
                f"recovery for {agent.agent!r} does not follow its manager/stuck route"
            )

    # Explicit card ownership is authoritative for intent/live/closure phases;
    # for the stable specialist phases, the generated route provides an extra
    # defense against assigning a developer as QA or a reviewer as implementer.
    if normalized_phase in {"execution", "technical_review", "activation"}:
        if normalized_phase not in route.accepted_phases:
            raise WorkforceOrganizationError(
                f"{agent.agent} does not accept lifecycle phase {normalized_phase}"
            )
    return agent


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate canonical Hermes workforce metadata")
    parser.add_argument("path", nargs="?", type=Path)
    parser.add_argument("--validate-profiles", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        org = load_organization(args.path, validate_profiles=args.validate_profiles)
        result = {
            "valid": True, "schema_version": org.schema_version,
            "agents": len(org.agents), "operational": len(org.operational_agents()),
            "source_path": str(org.source_path),
        }
    except WorkforceOrganizationError as exc:
        result = {"valid": False, "error": str(exc)}
    print(json.dumps(result, indent=2, sort_keys=True) if args.json else
          ("valid" if result["valid"] else f"invalid: {result['error']}"))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(_main())
