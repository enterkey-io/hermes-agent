#!/usr/bin/env python3
"""Compile canonical workforce metadata into staged profile contracts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

import yaml

from hermes_cli.workforce_org import WorkforceAgent, load_organization


BEGIN = "<!-- BEGIN MANAGED WORKFORCE CONTRACT -->"
END = "<!-- END MANAGED WORKFORCE CONTRACT -->"
BLOCK_RE = re.compile(rf"{re.escape(BEGIN)}.*?{re.escape(END)}", re.DOTALL)
PROTECTED = ("SOUL.md", "identity.md", "user.md")


def _list(items: tuple[str, ...]) -> str:
    return ", ".join(items) if items else "none"


def role_constraints(agent: WorkforceAgent) -> str:
    if agent.agent == "aurora":
        return "Aurora decides portfolio priority and routing within delegated authority; she does not cross Elliott's reserved gates."
    if agent.agent == "chloe":
        return ("Chloe is a directed observer and recorder. She may log facts and assemble explicitly requested material, "
                "but may not interpret, rank, recommend, prioritize, approve, route, manage, advise directors, or launch work.")
    if agent.agent == "mel":
        return "Mel develops alternatives and challenges assumptions; she may not approve, prioritize, route, assign, or execute implementation."
    if agent.agent == "grace":
        return "Grace owns executive support and directs Brenna and Milena, while coordinating with Aurora without taking department authority."
    if agent.agent == "brenna":
        return "Brenna remains LIFT-specific, reports through Grace, and is not a default admin-room member."
    if agent.agent == "milena":
        return "Milena coordinates for Grace. She and Chloe may exchange facts but may not assign work to each other."
    if agent.direct_reports:
        return "As a director or manager, I may direct routine work in my mandate and submit substantial opportunities; I do not review or veto Aurora."
    return "As a specialist, I execute routine work in my mandate and route new priorities through my manager."


def render_block(agent: WorkforceAgent, template: str, version: str) -> str:
    context = "\n".join([
        f"- Identity: {agent.display_name} (`{agent.agent}`)",
        f"- Manager: `{agent.manager}`",
        f"- Direct reports: {_list(agent.direct_reports)}",
        f"- Department/function: {agent.department or agent.function or 'cross-workforce'}",
        f"- Mission: {agent.mission}",
        f"- Owned outcomes: {_list(agent.owned_outcomes)}",
        f"- Authority: {_list(agent.authority)}",
        f"- Prohibited: {_list(agent.prohibited_actions)}",
        f"- Escalation target: `{agent.escalation_target}`",
        f"- Cross-team request path: {agent.cross_team_request_path or 'not applicable'}",
    ])
    body = template.replace("{{contract_version}}", version)
    body = body.replace("{{role_context}}", context)
    body = body.replace("{{role_constraints}}", role_constraints(agent))
    return f"{BEGIN}\n{body.strip()}\n{END}"


def insert_block(original: str, block: str) -> tuple[str, str]:
    if BLOCK_RE.search(original):
        return BLOCK_RE.sub(block, original), "replace"
    # First installation is purely additive: preserving the complete original
    # instruction file as an exact suffix gives the cutover validator a strong
    # byte-level proof that voice, relationship, privacy, and specialist rules
    # were not rewritten or flattened.
    return block + "\n\n" + original, "insert"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _compile_order(org) -> list[WorkforceAgent]:
    """Return the required top-down whole-workforce generation order."""
    selected: list[WorkforceAgent] = []
    queued = ["aurora", "grace"]
    seen: set[str] = set()
    while queued:
        agent_id = queued.pop(0)
        if agent_id in seen:
            continue
        item = org.get(agent_id)
        if item.operational and item.status in {"active", "planned"}:
            selected.append(item)
            seen.add(item.agent)
            queued.extend(item.direct_reports)
    selected.extend(
        item for item in org.operational_agents() if item.agent not in seen
    )
    return selected


def _protected_tree(profile: Path) -> dict[str, object]:
    roots = [*PROTECTED, "memories", "skills", "assets", "baselines"]
    records: list[tuple[str, str]] = []
    for name in roots:
        path = profile / name
        if path.is_file():
            records.append((name, sha(path)))
        elif path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_file():
                    records.append((str(child.relative_to(profile)), sha(child)))
    digest = hashlib.sha256(
        "\n".join(f"{name}\0{value}" for name, value in records).encode("utf-8")
    ).hexdigest()
    return {"file_count": len(records), "aggregate_sha256": digest}


def compile_profiles(
    org_path: Path,
    template_path: Path,
    output: Path,
    planned_source_root: Path | None = None,
) -> dict:
    org = load_organization(org_path)
    raw = yaml.safe_load(org_path.read_text(encoding="utf-8"))
    version = str(raw.get("workforce_contract_version") or org.schema_version)
    template = template_path.read_text(encoding="utf-8")
    output.mkdir(parents=True, exist_ok=True)
    entries = []
    for agent in _compile_order(org):
        target = output / agent.agent
        target.mkdir(parents=True, exist_ok=True)
        live_source = Path(agent.profile_path or "") / "AGENTS.md"
        planned_source = (
            planned_source_root / agent.agent / "AGENTS.md"
            if planned_source_root is not None else None
        )
        source = live_source
        source_kind = "live-profile"
        if (
            not live_source.is_file()
            and agent.status == "planned"
            and planned_source is not None
            and planned_source.is_file()
        ):
            source = planned_source
            source_kind = "planned-private-source"
        if source.is_file():
            original = source.read_text(encoding="utf-8-sig")
            candidate, operation = insert_block(original, render_block(agent, template, version))
            source_hash = sha(source)
        elif agent.status == "planned":
            original = f"# {agent.display_name} Operating Instructions\n"
            candidate, operation = insert_block(original, render_block(agent, template, version))
            source_hash = None
            source_kind = "generated-placeholder"
        else:
            raise FileNotFoundError(f"active profile instruction missing: {source}")
        candidate_path = target / "AGENTS.md"
        candidate_path.write_text(candidate, encoding="utf-8")
        protected: dict[str, object] = {"file_count": 0, "aggregate_sha256": None}
        if agent.profile_path:
            protected = _protected_tree(Path(agent.profile_path))
        entries.append({
            "agent": agent.agent, "status": agent.status,
            "source": str(source), "target": str(live_source),
            "source_kind": source_kind,
            "source_sha256": source_hash,
            "candidate": str(candidate_path), "candidate_sha256": sha(candidate_path),
            "operation": operation,
            "original_instruction_preserved_as_exact_suffix": candidate.endswith(original),
            "protected_tree": protected,
        })
    manifest = {"contract_version": version, "profiles": entries}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def render_organization_reference(org_path: Path) -> str:
    org = load_organization(org_path)
    lines = [
        "# Agent Organization and Dispatch Directory",
        "",
        "> Generated from canonical `organization.yaml`. Do not hand-edit.",
        "",
        "The workforce is proactive: each operational agent notices and completes the highest-value safe next step inside existing authority. New substantial work is signaled to Aurora, not launched independently.",
        "",
        "| Agent | Status | Department/function | Manager | Direct reports | Mission |",
        "|---|---|---|---|---|---|",
    ]
    for agent in org.agents.values():
        scope = agent.department or agent.function or "-"
        reports = ", ".join(agent.direct_reports) or "-"
        lines.append(
            f"| {agent.display_name} (`{agent.agent}`) | {agent.status} | {scope} | {agent.manager or '-'} | {reports} | {agent.mission} |"
        )
    lines.extend([
        "", "## Dispatch rules", "",
        "- Elliott may contact anyone directly; contact does not change reporting authority.",
        "- Routine, reversible, approved work is executed and verified without waiting for permission.",
        "- Substantial new work, cross-boundary commitments, and retained approvals go to Aurora.",
        "- Amy and Kourtnie are friends, not operational assignees. `default` is an artifact.",
        "- Buzz is conversation and delivery; Kanban, runbooks, Workflow Registry, Cron, and repositories remain durable authority.",
    ])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--organization", type=Path, required=True)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--organization-reference", type=Path)
    parser.add_argument("--manifest-report", type=Path)
    parser.add_argument(
        "--planned-source-root",
        type=Path,
        help="owner-only source profiles for planned agents with no live profile",
    )
    args = parser.parse_args()
    manifest = compile_profiles(
        args.organization,
        args.template,
        args.output,
        planned_source_root=args.planned_source_root,
    )
    if args.organization_reference:
        args.organization_reference.parent.mkdir(parents=True, exist_ok=True)
        args.organization_reference.write_text(
            render_organization_reference(args.organization), encoding="utf-8"
        )
    if args.manifest_report:
        args.manifest_report.parent.mkdir(parents=True, exist_ok=True)
        args.manifest_report.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        args.manifest_report.chmod(0o600)
    print(json.dumps({"profiles": len(manifest["profiles"]), "contract_version": manifest["contract_version"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
