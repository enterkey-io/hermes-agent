#!/usr/bin/env python3
"""Stage plugin/toolset config candidates without changing live profiles."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import Any

import yaml

from hermes_cli.workforce_org import load_organization


_HEX_KEY = re.compile(r"^[0-9a-fA-F]{64}$")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _apply_auxiliary_policy(raw: dict[str, Any], registry: dict[str, Any]) -> list[str]:
    """Apply explicit auxiliary routes without replacing task-specific timeouts."""
    changes: list[str] = []
    auxiliary = raw.setdefault("auxiliary", {})
    policies = registry.get("auxiliary_policy") or {}
    for policy_name in ("general_low_cost", "vision"):
        policy = policies.get(policy_name) or {}
        for task in policy.get("tasks") or []:
            slot = auxiliary.setdefault(str(task), {})
            slot["provider"] = str(policy["provider"])
            slot["model"] = str(policy["model"])
            slot["reasoning_effort"] = str(policy["reasoning_effort"])
            if task == "background_review":
                slot["enabled"] = True
            changes.append(
                f"auxiliary.{task}:{policy['model']}/{policy['reasoning_effort']}"
            )
    return changes


def _dotenv_value(path: Path, key: str) -> str | None:
    """Read one simple credential value without loading other profile secrets."""
    if not path.is_file():
        return None
    prefix = f"{key}="
    found: str | None = None
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        stripped = line.strip()
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        if not stripped.startswith(prefix):
            continue
        value = stripped[len(prefix):].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if found is not None:
            raise ValueError(f"{path}: duplicate {key} definitions")
        found = value
    return found


def _remove_migrated_config_secrets(raw: dict[str, Any], profile: Path) -> list[str]:
    """Remove a legacy top-level Buzz key only after proving its env migration."""
    if "BUZZ_PRIVATE_KEY" not in raw:
        return []
    legacy = str(raw["BUZZ_PRIVATE_KEY"] or "").strip()
    env_value = _dotenv_value(profile / ".env", "BUZZ_PRIVATE_KEY")
    if not _HEX_KEY.fullmatch(legacy):
        raise ValueError(f"{profile.name}: legacy BUZZ_PRIVATE_KEY is not a 64-character hex key")
    if env_value != legacy:
        raise RuntimeError(
            f"{profile.name}: migrate the exact legacy BUZZ_PRIVATE_KEY to the owner-only .env before staging"
        )
    raw.pop("BUZZ_PRIVATE_KEY")
    return ["credential-location:BUZZ_PRIVATE_KEY:config-to-env"]


def stage(organization: Path, output: Path, models_path: Path | None = None) -> dict[str, Any]:
    org = load_organization(organization, validate_profiles=True)
    model_registry = yaml.safe_load(models_path.read_text(encoding="utf-8-sig")) if models_path else None
    rows = []
    for agent in org.operational_agents(include_planned=False):
        profile = Path(str(agent.profile_path))
        source = profile / "config.yaml"
        raw = yaml.safe_load(source.read_text(encoding="utf-8-sig")) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"{agent.agent}: config must be a mapping")
        secret_changes = _remove_migrated_config_secrets(raw, profile)
        toolsets = list(raw.get("toolsets") or [])
        if "workforce" not in toolsets:
            toolsets.append("workforce")
        raw["toolsets"] = toolsets
        plugins = raw.setdefault("plugins", {})
        enabled = list(plugins.get("enabled") or [])
        disabled = list(plugins.get("disabled") or [])
        if "workforce-control" not in enabled:
            enabled.append("workforce-control")
        disabled = [name for name in disabled if name != "workforce-control"]
        plugins["enabled"] = enabled
        plugins["disabled"] = disabled
        managed_change = [
            "toolsets:+workforce", "plugins.enabled:+workforce-control", *secret_changes
        ]
        kanban = raw.setdefault("kanban", {})
        kanban["auto_decompose"] = False
        kanban["dispatch_in_gateway"] = agent.agent == "root"
        managed_change.extend([
            "kanban.auto_decompose:false",
            f"kanban.dispatch_in_gateway:{str(agent.agent == 'root').lower()}",
        ])
        if model_registry:
            assignment = (model_registry.get("assignments") or {}).get(agent.agent)
            if not assignment:
                raise ValueError(f"{agent.agent}: missing approved model assignment")
            if str(assignment.get("profile")) != profile.name:
                raise ValueError(f"{agent.agent}: model assignment profile mismatch")
            preset_name = str(assignment.get("preset") or "")
            preset = (model_registry.get("presets") or {}).get(preset_name)
            if not preset:
                raise ValueError(f"{agent.agent}: unknown model preset {preset_name}")
            model = raw.setdefault("model", {})
            model["provider"] = preset["provider"]
            model["default"] = preset["model"]
            raw.setdefault("agent", {})["reasoning_effort"] = preset["reasoning_effort"]
            platform_models = raw.setdefault("platform_models", {})
            platform_models["matrix"] = {
                "provider": "ollama-cloud", "default": "glm-5.2:cloud",
                "reasoning_effort": "medium",
            }
            platform_models.pop("buzz", None)
            managed_change.extend(_apply_auxiliary_policy(raw, model_registry))
            managed_change.extend([
                f"model:{preset_name}", "platform_models.matrix:glm-5.2:cloud/medium",
                "platform_models.buzz:inherit-main",
            ])
        candidate = output / profile.name / "config.yaml"
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
        candidate.chmod(0o600)
        rows.append({
            "agent": agent.agent,
            "status": agent.status,
            "source": str(source),
            "target": str(source),
            "source_sha256": _sha(source),
            "candidate": str(candidate),
            "candidate_sha256": _sha(candidate),
            "managed_change": managed_change,
        })
    manifest = {"schema_version": 1, "mutation_performed": False, "profiles": rows}
    path = output / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--organization", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--models", type=Path)
    args = parser.parse_args(argv)
    result = stage(args.organization, args.output, args.models)
    print(json.dumps({"profiles": len(result["profiles"]), "mutation_performed": False}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
