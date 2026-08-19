#!/usr/bin/env python3
"""Run the deterministic Workforce Control observer as a recovery sweep."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from hermes_cli import kanban_db
from plugins.workforce_control.store import observe_dispatch_tick


def run(database: Path, *, observer: str = "dispatch_tick") -> dict:
    """Consume missed classified events without initializing or applying state."""
    with kanban_db.connect_closing(database) as conn:
        return observe_dispatch_tick(conn, observer=observer)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--observer", default="dispatch_tick")
    args = parser.parse_args(argv)
    result = run(args.database, observer=args.observer)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
