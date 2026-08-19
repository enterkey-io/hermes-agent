#!/usr/bin/env python3
"""Prove generated workforce blocks preserve each prior instruction file exactly."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from scripts.workforce_compile import BEGIN, BLOCK_RE, END


def build(manifest_path: Path) -> tuple[dict, str]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = []
    for item in manifest["profiles"]:
        source = Path(item["source"]).read_text(encoding="utf-8-sig")
        candidate = Path(item["candidate"]).read_text(encoding="utf-8-sig")
        preserved = (
            BLOCK_RE.sub("", candidate) == BLOCK_RE.sub("", source)
            if BEGIN in source
            else candidate.endswith(source)
        )
        managed_once = candidate.count(BEGIN) == 1 and candidate.count(END) == 1
        rows.append({
            "agent": item["agent"],
            "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "unmanaged_content_byte_equivalent": preserved,
            "one_managed_block": managed_once,
        })
    valid = all(row["unmanaged_content_byte_equivalent"] and row["one_managed_block"] for row in rows)
    report = {"schema_version": 1, "valid": valid, "profiles": len(rows), "results": rows}
    lines = [
        "# Protected profile-content preservation", "",
        "The candidate generator added or replaced one managed workforce block and retained",
        "all unmanaged operational `AGENTS.md` content byte-equivalently. No identity, voice,",
        "relationship, memory, privacy, channel, or specialist text is reproduced in",
        "this report.", "",
        f"Result: **{'PASS' if valid else 'FAIL'}** across {len(rows)} operational profiles.", "",
        "| Agent | Source SHA-256 | Unmanaged content preserved | One managed block |", "|---|---|---:|---:|",
    ]
    for row in rows:
        lines.append(f"| {row['agent']} | `{row['source_sha256']}` | {row['unmanaged_content_byte_equivalent']} | {row['one_managed_block']} |")
    return report, "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    args = parser.parse_args(argv)
    report, markdown = build(args.manifest)
    for path, text in ((args.json_output, json.dumps(report, indent=2, sort_keys=True) + "\n"), (args.markdown_output, markdown)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)
    print(json.dumps({"valid": report["valid"], "profiles": report["profiles"]}, indent=2))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
