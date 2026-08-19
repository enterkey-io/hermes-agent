from __future__ import annotations

from pathlib import Path

import yaml


def materialize_test_organization(source: Path, root: Path) -> Path:
    """Create a hermetic organization whose active profiles have fixtures."""
    data = yaml.safe_load(source.read_text(encoding="utf-8"))
    profiles = root / "workforce-profiles"
    for item in data["agents"]:
        if not item.get("operational") or item.get("status") != "active":
            continue
        profile_name = "main" if item["agent"] == "root" else item["agent"]
        profile = profiles / profile_name
        profile.mkdir(parents=True, exist_ok=True)
        instructions = profile / "AGENTS.md"
        instructions.write_text(
            f"# {item['display_name']} test instructions\n\n"
            "Private identity and voice fixture.\n",
            encoding="utf-8",
        )
        item["profile_path"] = str(profile)
    output = root / "organization.yaml"
    output.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return output
