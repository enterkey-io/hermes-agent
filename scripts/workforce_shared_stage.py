#!/usr/bin/env python3
"""Stage shared canonical organization files for guarded cutover."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from scripts.workforce_compile import render_organization_reference


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stage(organization: Path, output: Path, target_root: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    org_candidate = output / "organization.yaml"
    org_candidate.write_bytes(organization.read_bytes())
    ref_candidate = output / "AGENT_ORGANIZATION.md"
    ref_candidate.write_text(render_organization_reference(organization), encoding="utf-8")
    rows = []
    for name, candidate in (("organization.yaml", org_candidate), ("AGENT_ORGANIZATION.md", ref_candidate)):
        target = target_root / name
        exists = target.is_file()
        rows.append({
            "agent": f"shared:{name}", "status": "active",
            "source": str(target), "target": str(target),
            "source_sha256": _sha(target) if exists else None,
            "candidate": str(candidate), "candidate_sha256": _sha(candidate),
            "create_if_missing": not exists,
        })
        candidate.chmod(0o600)
    manifest = {"schema_version": 1, "mutation_performed": False, "profiles": rows}
    path = output / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--organization", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target-root", type=Path, required=True)
    args = parser.parse_args(argv)
    result = stage(args.organization, args.output, args.target_root)
    print(json.dumps({"files": len(result["profiles"]), "mutation_performed": False}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
