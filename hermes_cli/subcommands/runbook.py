"""``hermes runbook`` control-plane parser.

Only reviewed-proposal activation is exposed here.  Proposal authoring remains
agent/tool-driven, while this command provides an explicit, auditable operator
surface for the narrow activation transition.
"""

from __future__ import annotations

from typing import Callable


def build_runbook_parser(subparsers, *, cmd_runbook: Callable) -> None:
    """Attach the audited reviewed-proposal activation command."""
    parser = subparsers.add_parser(
        "runbook",
        help="Operate canonical runbook control-plane actions",
    )
    actions = parser.add_subparsers(dest="runbook_command", metavar="<subcommand>")
    activate = actions.add_parser(
        "activate-reviewed",
        help="Activate one reviewed proposal with bound approval evidence",
        description=(
            "Activates exactly one reviewed proposal after checking its slug, "
            "proposal ID and SHA-256, current active revision, and Elliott's "
            "recorded legacy approval or an independent internal reviewer "
            "attestation. This updates the Workflow Registry "
            "projection but never creates or mutates cron jobs."
        ),
    )
    activate.add_argument("--slug", required=True, help="Canonical runbook slug")
    activate.add_argument(
        "--proposal-id",
        required=True,
        help="Proposal filename stem (without .md/.json)",
    )
    activate.add_argument(
        "--proposal-sha256",
        required=True,
        help="Exact SHA-256 of the reviewed proposal Markdown",
    )
    activate.add_argument(
        "--expected-active-revision",
        required=True,
        help="Exact current canonical revision, or literal 'absent' for an audited create",
    )
    activate.add_argument(
        "--operator",
        required=True,
        help="Accountable operator identity performing this activation",
    )
    activate.add_argument(
        "--approval-evidence",
        required=True,
        help="Owner-signed or reviewer-attested JSON evidence file retained in the audit event",
    )
    activate.set_defaults(func=cmd_runbook)
    parser.set_defaults(func=cmd_runbook)
