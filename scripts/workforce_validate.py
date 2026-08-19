#!/usr/bin/env python3
"""Validate the staged workforce as one coordinated organization."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import tempfile
from typing import Any

from hermes_cli.workforce_org import load_organization
from scripts.workforce_compile import BEGIN, END


REQUIRED_CONTRACT_SIGNALS = (
    "Do Smart Things: proactive execution",
    "I do not wait for Elliott to originate every useful step",
    "Routine approved execution",
    "Substantial new work",
    "Every accepted handoff",
    "marked stalled and reported to Aurora and Chloe",
    "Buzz is focused conversation and operational delivery",
    "Keep routine success quiet",
    "Proactivity begins with understanding",
    "the work remains in discovery",
    "Mobilize the smallest useful tranche",
    "stop the affected execution first",
    "activity from outcome",
)

# These files are maintained by the running skill-curator/usage telemetry, so
# their hashes and mtimes can move even while a friend profile is completely
# outside this project.  Identity, instructions, skill definitions, and all
# other durable profile files remain protected by the comparison below.
FRIEND_RUNTIME_MUTABLE_SUFFIXES = (
    "/channel_directory.json",
    "/config.non-secret.yaml",
    "/skills/.curator_state",
    "/skills/.usage.json",
    "/cron/jobs.json",
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _friend_preservation(
    backup_manifest: Path, profiles_root: Path
) -> dict[str, Any]:
    backup = json.loads(backup_manifest.read_text(encoding="utf-8"))
    protected = ("profiles/amy/", "profiles/kourtnie/")
    mismatches: list[dict[str, str]] = []
    checked = 0
    for record in backup.get("files", []):
        relative = str(record.get("path") or "")
        if not relative.startswith(protected) or record.get("type") != "file":
            continue
        if relative.endswith(FRIEND_RUNTIME_MUTABLE_SUFFIXES):
            continue
        checked += 1
        current = profiles_root.parent / relative
        if not current.is_file():
            mismatches.append({"path": relative, "reason": "missing"})
            continue
        stat = current.stat()
        if _sha(current) != record.get("sha256"):
            mismatches.append({"path": relative, "reason": "hash"})
        if stat.st_mtime_ns != int(record.get("mtime_ns")):
            mismatches.append({"path": relative, "reason": "mtime"})
    return {
        "checked_files": checked,
        "mismatches": mismatches,
        "valid": checked > 0 and not mismatches,
    }


def validate(
    *,
    organization: Path,
    staging_root: Path,
    profiles_root: Path,
    backup_manifest: Path,
    delivery_manifest: Path,
) -> dict[str, Any]:
    org = load_organization(organization)
    manifest = json.loads((staging_root / "manifest.json").read_text(encoding="utf-8"))
    expected = [item.agent for item in org.operational_agents()]
    actual = [str(item["agent"]) for item in manifest["profiles"]]
    profile_results: list[dict[str, Any]] = []
    errors: list[str] = []
    if set(actual) != set(expected):
        errors.append("staged profile set does not match the operational organization")
    if actual[:2] != ["aurora", "grace"]:
        errors.append("top-down staging must begin with Aurora and Grace")
    for item in manifest["profiles"]:
        agent = org.get(item["agent"])
        candidate = Path(item["candidate"])
        text = candidate.read_text(encoding="utf-8")
        normalized = re.sub(r"\s+", " ", text).casefold()
        from agent.prompt_builder import build_context_files_prompt

        with tempfile.TemporaryDirectory(prefix=f"workforce-{agent.agent}-") as scratch:
            (Path(scratch) / "AGENTS.md").write_text(text, encoding="utf-8")
            runtime_prompt = build_context_files_prompt(
                cwd=scratch,
                skip_soul=True,
                context_length=200_000,
                allow_install_tree_fallback=True,
            )
        missing = [
            signal
            for signal in REQUIRED_CONTRACT_SIGNALS
            if re.sub(r"\s+", " ", signal).casefold() not in normalized
        ]
        result = {
            "agent": agent.agent,
            "status": agent.status,
            "managed_block_count": text.count(BEGIN),
            "managed_end_count": text.count(END),
            "missing_contract_signals": missing,
            "manager_present": f"- Manager: `{agent.manager}`" in text,
            "runtime_prompt_loaded": (
                "Do Smart Things: proactive execution" in runtime_prompt
                and f"- Manager: `{agent.manager}`" in runtime_prompt
            ),
            "source_hash_current": None,
            "source_hash_matches": None,
            "original_instruction_preserved_as_exact_suffix": item[
                "original_instruction_preserved_as_exact_suffix"
            ],
        }
        source = Path(item["source"])
        if source.is_file():
            result["source_hash_current"] = _sha(source)
            result["source_hash_matches"] = result["source_hash_current"] == item["source_sha256"]
        if (
            result["managed_block_count"] != 1
            or result["managed_end_count"] != 1
            or missing
            or not result["manager_present"]
            or not result["runtime_prompt_loaded"]
            or result["source_hash_matches"] is False
            or not result["original_instruction_preserved_as_exact_suffix"]
        ):
            errors.append(f"{agent.agent}: staged contract validation failed")
        profile_results.append(result)
    chloe_text = (staging_root / "chloe" / "AGENTS.md").read_text(encoding="utf-8")
    chloe_boundaries = all(
        token in chloe_text
        for token in ("may not interpret", "recommend", "prioritize", "route", "launch work")
    )
    mel_text = (staging_root / "mel" / "AGENTS.md").read_text(encoding="utf-8")
    mel_boundaries = "may not approve, prioritize, route, assign, or execute" in mel_text
    if not chloe_boundaries:
        errors.append("Chloe boundary contract is incomplete")
    if not mel_boundaries:
        errors.append("Mel boundary contract is incomplete")
    friends = _friend_preservation(backup_manifest, profiles_root)
    if not friends["valid"]:
        errors.append("friend profile content or mtimes drifted")
    delivery = json.loads(delivery_manifest.read_text(encoding="utf-8"))
    if not delivery.get("valid") or delivery.get("summary", {}).get("unclassified"):
        errors.append("delivery inventory contains unclassified operational jobs")
    planned = [
        item["agent"] for item in manifest["profiles"] if item["status"] == "planned"
    ]
    cutover_gates = []
    if "chloe" in planned:
        cutover_gates.append(
            "Chloe profile onboarding and Elliott-approved personal canon are incomplete"
        )
    registry_mismatches = int(
        (delivery.get("summary") or {}).get("registry_cron_mismatches") or 0
    )
    if registry_mismatches:
        cutover_gates.append(
            f"{registry_mismatches} enabled Cron jobs require reviewed Registry/runbook links"
        )
    cutover_gates.extend(
        [
            "Buzz rooms and room UUID map require explicit creation authorization",
            "production cutover requires separate explicit authorization",
        ]
    )
    return {
        "valid": not errors,
        "errors": errors,
        "organization_agents": len(org.agents),
        "operational_profiles": len(expected),
        "profile_results": profile_results,
        "role_boundaries": {
            "chloe_directed_observer_only": chloe_boundaries,
            "mel_nonexecuting_vision_only": mel_boundaries,
            "friends_excluded": friends["valid"],
            "default_excluded": "default" not in actual,
        },
        "friend_preservation": friends,
        "delivery_summary": delivery.get("summary"),
        "staged_implementation_ready": not errors,
        "whole_workforce_cutover_ready": not errors and not planned and not registry_mismatches,
        "cutover_gates": cutover_gates,
        "live_cutover_authorized": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--organization", type=Path, required=True)
    parser.add_argument("--staging-root", type=Path, required=True)
    parser.add_argument("--profiles-root", type=Path, required=True)
    parser.add_argument("--backup-manifest", type=Path, required=True)
    parser.add_argument("--delivery-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = validate(
            organization=args.organization,
            staging_root=args.staging_root,
            profiles_root=args.profiles_root,
            backup_manifest=args.backup_manifest,
            delivery_manifest=args.delivery_manifest,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        report = {"valid": False, "errors": [str(exc)], "live_cutover_authorized": False}
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        args.output.chmod(0o600)
    print(rendered, end="")
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
