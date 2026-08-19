#!/usr/bin/env python3
"""Stage plugin/toolset config candidates without changing live profiles."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from hermes_cli.workforce_org import load_organization


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
        managed_change = ["toolsets:+workforce", "plugins.enabled:+workforce-control"]
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
