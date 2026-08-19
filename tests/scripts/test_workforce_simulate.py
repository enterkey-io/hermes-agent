from pathlib import Path

from scripts.workforce_compile import compile_profiles
from scripts.workforce_simulate import simulate
from tests.workforce_test_helpers import materialize_test_organization


ROOT = Path(__file__).parents[2]


def test_staged_whole_workforce_passes_proactive_authority_simulation(tmp_path: Path):
    staging = tmp_path / "staging"
    organization = materialize_test_organization(
        ROOT / "workforce/organization.yaml", tmp_path
    )
    compile_profiles(
        organization,
        ROOT / "workforce/templates/workforce-contract.md",
        staging,
    )
    result = simulate(
        organization,
        staging,
    )
    assert result["valid"] is True
    assert result["profiles_simulated"] == 22
    assert all(result["interactions"].values())
    assert all(
        item["routine_approved_work"] == "execute_verify_close"
        and item["reserved_action"] == "escalate_gate_continue_safe_work"
        for item in result["profile_results"]
    )
    aurora = next(item for item in result["profile_results"] if item["agent"] == "aurora")
    assert aurora["substantial_new_work"] == "aurora_decides_delegates_and_follows_through"
    assert all(
        item["substantial_new_work"] == "signal_aurora_do_not_launch"
        for item in result["profile_results"]
        if item["agent"] != "aurora"
    )
