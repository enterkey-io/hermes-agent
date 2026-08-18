import importlib.util
from pathlib import Path

import yaml


ROOT = Path(__file__).parents[2]
SPEC = importlib.util.spec_from_file_location(
    "workforce_compile", ROOT / "scripts" / "workforce_compile.py"
)
module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(module)


def test_insert_is_idempotent_and_preserves_unmanaged_content():
    original = "# Agent\n\nPrivate voice stays here.\n"
    block = f"{module.BEGIN}\ncontract\n{module.END}"
    first, operation = module.insert_block(original, block)
    second, replacement = module.insert_block(first, block)
    assert operation == "insert"
    assert replacement == "replace"
    assert first == second
    assert "Private voice stays here." in first
    assert first.endswith(original)
    assert first.count(module.BEGIN) == 1


def test_canonical_compile_includes_active_chloe(tmp_path):
    manifest = module.compile_profiles(
        ROOT / "workforce" / "organization.yaml",
        ROOT / "workforce" / "templates" / "workforce-contract.md",
        tmp_path,
    )
    assert len(manifest["profiles"]) == 21
    assert [item["agent"] for item in manifest["profiles"][:2]] == ["aurora", "grace"]
    assert all(item["original_instruction_preserved_as_exact_suffix"] for item in manifest["profiles"])
    chloe = next(item for item in manifest["profiles"] if item["agent"] == "chloe")
    assert chloe["status"] == "active"
    assert chloe["source_kind"] == "live-profile"
    assert chloe["source"] == "/home/elliott/.hermes/profiles/chloe/AGENTS.md"
    assert chloe["target"] == "/home/elliott/.hermes/profiles/chloe/AGENTS.md"
    text = (tmp_path / "chloe" / "AGENTS.md").read_text()
    assert "may not interpret, rank, recommend" in text
    assert module.BEGIN in text

    aurora_text = (tmp_path / "aurora" / "AGENTS.md").read_text()
    assert "translate that intent into execution" in aurora_text
    assert "delegate the rest to the right owner" in aurora_text
    assert "follow through until the outcome is completed" in aurora_text
    assert "I do not send a signal to myself" in aurora_text
    assert "submit one concrete workforce signal for Aurora" not in aurora_text


def test_planned_profile_can_use_owner_only_private_source(tmp_path):
    organization = yaml.safe_load(
        (ROOT / "workforce" / "organization.yaml").read_text(encoding="utf-8")
    )
    chloe = next(item for item in organization["agents"] if item["agent"] == "chloe")
    chloe["status"] = "planned"
    chloe["profile_path"] = str(tmp_path / "missing-live-profile")
    organization_path = tmp_path / "organization.yaml"
    organization_path.write_text(
        yaml.safe_dump(organization, sort_keys=False), encoding="utf-8"
    )
    planned = tmp_path / "planned"
    source = planned / "chloe" / "AGENTS.md"
    source.parent.mkdir(parents=True)
    source.write_text("# Chloe private source\n\nHer established voice.\n")
    output = tmp_path / "output"
    manifest = module.compile_profiles(
        organization_path,
        ROOT / "workforce" / "templates" / "workforce-contract.md",
        output,
        planned_source_root=planned,
    )
    entry = next(item for item in manifest["profiles"] if item["agent"] == "chloe")
    assert entry["source_kind"] == "planned-private-source"
    assert entry["target"] == str(tmp_path / "missing-live-profile" / "AGENTS.md")
    assert entry["source_sha256"]
    assert (output / "chloe" / "AGENTS.md").read_text().endswith(
        source.read_text()
    )


def test_generated_reference_excludes_friends_from_dispatch():
    rendered = module.render_organization_reference(
        ROOT / "workforce" / "organization.yaml"
    )
    assert "The workforce is proactive" in rendered
    assert "Amy (`amy`) | friend" in rendered
    assert "friends, not operational assignees" in rendered
