#!/usr/bin/env python3
"""Run deterministic whole-workforce authority and initiative simulations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Any

from hermes_cli.workforce_handoffs import _authorized_route
from hermes_cli.workforce_org import load_organization


SAFE_ROUTINE_SIGNALS = (
    "choose the highest-value safe next action inside my role",
    "do routine reversible work",
    "verify the result",
    "close the loop",
)
SUBSTANTIAL_SIGNALS = (
    "I do not launch it",
    "submit one concrete workforce signal for Aurora",
    "decision ownership",
)
RESERVED_SIGNALS = (
    "spending",
    "real-money actions",
    "public publication",
    "credential or security changes",
    "pause only the gated step",
    "Continue unrelated safe work",
)
DURABLE_SIGNALS = (
    "Active work, ownership, handoffs, dependencies, and signals belong in Hermes Kanban",
    "Recurring procedures belong in the Workflow Registry, canonical runbooks, and Hermes Cron",
    "Buzz is focused conversation and operational delivery, not the durable source of truth",
)


def simulate(organization: Path, staging_root: Path) -> dict[str, Any]:
    org = load_organization(organization)
    results = []
    errors: list[str] = []
    for agent in org.operational_agents():
        path = staging_root / agent.agent / "AGENTS.md"
        text = path.read_text(encoding="utf-8")
        normalized = re.sub(r"\s+", " ", text).casefold()
        contains = lambda signals: all(
            re.sub(r"\s+", " ", signal).casefold() in normalized for signal in signals
        )
        safe = contains(SAFE_ROUTINE_SIGNALS)
        substantial = contains(SUBSTANTIAL_SIGNALS)
        reserved = contains(RESERVED_SIGNALS)
        durable = contains(DURABLE_SIGNALS)
        manager_route = agent.manager == "elliott"
        if not manager_route:
            try:
                _authorized_route(org, agent.manager or "", agent.agent)
                manager_route = True
            except ValueError:
                manager_route = False
        result = {
            "agent": agent.agent,
            "status": agent.status,
            "routine_approved_work": "execute_verify_close" if safe else "failed",
            "substantial_new_work": "signal_aurora_do_not_launch" if substantial else "failed",
            "reserved_action": "escalate_gate_continue_safe_work" if reserved else "failed",
            "durable_record_routing": "canonical_systems" if durable else "failed",
            "manager_can_assign": manager_route,
        }
        if not all((safe, substantial, reserved, durable, manager_route)):
            errors.append(f"{agent.agent}: scenario simulation failed")
        results.append(result)

    interaction_results: dict[str, bool] = {}
    try:
        _authorized_route(org, "aurora", "xenia")
        interaction_results["aurora_can_route_cross_department"] = True
    except ValueError:
        interaction_results["aurora_can_route_cross_department"] = False
    try:
        _authorized_route(org, "emily", "xenia")
        interaction_results["director_cross_department_blocked"] = False
    except ValueError:
        interaction_results["director_cross_department_blocked"] = True
    try:
        _authorized_route(org, "emily", "sage")
        interaction_results["director_can_route_to_direct_report"] = True
    except ValueError:
        interaction_results["director_can_route_to_direct_report"] = False

    chloe_text = (staging_root / "chloe" / "AGENTS.md").read_text(encoding="utf-8")
    interaction_results["chloe_observes_without_deciding"] = all(
        value in chloe_text
        for value in (
            "directed observer and recorder",
            "may not interpret",
            "prioritize",
            "approve",
            "route",
            "launch work",
        )
    )
    mel_text = (staging_root / "mel" / "AGENTS.md").read_text(encoding="utf-8")
    interaction_results["mel_vision_without_execution_authority"] = (
        "may not approve, prioritize, route, assign, or execute implementation" in mel_text
    )
    if not all(interaction_results.values()):
        errors.append("cross-workforce interaction simulation failed")
    return {
        "valid": not errors,
        "mutation_performed": False,
        "profiles_simulated": len(results),
        "profile_results": results,
        "interactions": interaction_results,
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--organization", type=Path, required=True)
    parser.add_argument("--staging-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = simulate(args.organization, args.staging_root)
    except (OSError, ValueError) as exc:
        report = {"valid": False, "mutation_performed": False, "errors": [str(exc)]}
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        args.output.chmod(0o600)
    print(rendered, end="")
    return 0 if report.get("valid") else 1


if __name__ == "__main__":
    raise SystemExit(main())
