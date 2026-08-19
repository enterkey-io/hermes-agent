#!/usr/bin/env python3
"""Combine staged instruction and config manifests for one atomic cutover."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def bundle(instructions: Path, configs: Path, output: Path, shared: Path | None = None) -> dict:
    instruction_rows = json.loads(instructions.read_text(encoding="utf-8"))["profiles"]
    config_rows = json.loads(configs.read_text(encoding="utf-8"))["profiles"]
    by_agent = {str(row["agent"]): row for row in config_rows}
    rows = []
    for row in instruction_rows:
        agent = str(row["agent"])
        if agent not in by_agent:
            raise ValueError(f"missing config candidate for {agent}")
        instruction = dict(row)
        instruction["agent"] = f"{agent}:instructions"
        rows.append(instruction)
        config = dict(by_agent.pop(agent))
        config["agent"] = f"{agent}:config"
        rows.append(config)
    if by_agent:
        raise ValueError("config candidates have no instruction peer: " + ", ".join(sorted(by_agent)))
    if shared:
        rows.extend(json.loads(shared.read_text(encoding="utf-8"))["profiles"])
    result = {
        "schema_version": 1,
        "mutation_performed": False,
        "logical_profiles": len(instruction_rows),
        "writes": len(rows),
        "profiles": rows,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output.chmod(0o600)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instructions", type=Path, required=True)
    parser.add_argument("--configs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--shared", type=Path)
    args = parser.parse_args(argv)
    result = bundle(args.instructions, args.configs, args.output, args.shared)
    print(json.dumps({"logical_profiles": result["logical_profiles"], "writes": result["writes"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
