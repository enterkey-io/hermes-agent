from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli.workforce_org import (
    WorkforceOrganizationError,
    derive_lifecycle_route,
    load_organization,
    validate_lifecycle_assignment,
)


ROOT = Path(__file__).parents[2]


@pytest.fixture(scope="module")
def org():
    return load_organization(ROOT / "workforce" / "organization.yaml")


def test_technical_routes_come_from_canonical_ownership(org) -> None:
    sloane = derive_lifecycle_route(org, "sloane")
    reese = derive_lifecycle_route(org, "reese")

    assert sloane.manager == "emily"
    assert sloane.normal_receiver == "reese"
    assert sloane.technical_reviewer == org.technical_ownership["qa"]
    assert sloane.stuck_route == "emily"
    assert "execution" in sloane.accepted_phases

    assert reese.normal_receiver == "intent_validator"
    assert reese.failure_receiver == "implementer"
    assert "technical_review" in reese.accepted_phases
    assert "execution" not in reese.accepted_phases


@pytest.mark.parametrize(
    ("agent", "receiver"),
    [
        ("sage", "emily"),
        ("iris", "emily"),
        ("margot", "bridgette"),
        ("kenzie", "bridgette"),
        ("emma", "bridgette"),
        ("brenna", "grace"),
        ("milena", "grace"),
        ("oyku", "xenia"),
        ("mel", "aurora"),
        ("chloe", "aurora"),
    ],
)
def test_specialist_receivers_follow_org_management(org, agent, receiver) -> None:
    route = derive_lifecycle_route(org, agent)
    assert route.normal_receiver == receiver
    assert route.stuck_route == receiver


def test_activation_boundaries_come_from_technical_ownership(org) -> None:
    route = derive_lifecycle_route(org, "aurora")
    assert route.local_activation_owner == "alina"
    assert route.external_activation_owner == "root"
    assert route.local_activation_owner != route.external_activation_owner

    assert "activation" in derive_lifecycle_route(org, "alina").accepted_phases
    assert "activation" in derive_lifecycle_route(org, "root").accepted_phases
    assert "activation" not in derive_lifecycle_route(org, "sloane").accepted_phases


def test_personal_and_artifact_profiles_have_no_lifecycle_route(org) -> None:
    for agent in ("elliott", "amy", "kourtnie", "default"):
        with pytest.raises(WorkforceOrganizationError):
            derive_lifecycle_route(org, agent)


def test_phase_assignment_is_bound_to_recorded_role(org) -> None:
    roles = {
        "implementer": "sloane",
        "technical_reviewer": "reese",
        "intent_validator": "aurora",
        "activation_owner": "alina",
        "closure_owner": "aurora",
    }
    assert validate_lifecycle_assignment(org, "sloane", "execution", roles).agent == "sloane"
    assert validate_lifecycle_assignment(org, "reese", "technical_review", roles).agent == "reese"
    assert validate_lifecycle_assignment(org, "aurora", "intent_review", roles).agent == "aurora"
    assert validate_lifecycle_assignment(org, "alina", "activation", roles).agent == "alina"
    assert validate_lifecycle_assignment(org, "aurora", "closure", roles).agent == "aurora"

    with pytest.raises(WorkforceOrganizationError, match="technical_reviewer"):
        validate_lifecycle_assignment(org, "sloane", "technical_review", roles)
