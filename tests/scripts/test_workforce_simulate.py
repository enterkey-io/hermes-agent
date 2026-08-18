from pathlib import Path

from scripts.workforce_simulate import simulate


ROOT = Path(__file__).parents[2]


def test_staged_whole_workforce_passes_proactive_authority_simulation():
    result = simulate(
        ROOT / "workforce/organization.yaml",
        ROOT / ".hermes/staging/profiles",
    )
    assert result["valid"] is True
    assert result["profiles_simulated"] == 21
    assert all(result["interactions"].values())
    assert all(
        item["routine_approved_work"] == "execute_verify_close"
        and item["substantial_new_work"] == "signal_aurora_do_not_launch"
        and item["reserved_action"] == "escalate_gate_continue_safe_work"
        for item in result["profile_results"]
    )
