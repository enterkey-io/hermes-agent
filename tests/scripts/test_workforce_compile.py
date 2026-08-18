import importlib.util
from pathlib import Path


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


def test_canonical_compile_includes_planned_chloe(tmp_path):
    manifest = module.compile_profiles(
        ROOT / "workforce" / "organization.yaml",
        ROOT / "workforce" / "templates" / "workforce-contract.md",
        tmp_path,
    )
    assert len(manifest["profiles"]) == 21
    assert [item["agent"] for item in manifest["profiles"][:2]] == ["aurora", "grace"]
    assert all(item["original_instruction_preserved_as_exact_suffix"] for item in manifest["profiles"])
    chloe = next(item for item in manifest["profiles"] if item["agent"] == "chloe")
    assert chloe["status"] == "planned"
    text = (tmp_path / "chloe" / "AGENTS.md").read_text()
    assert "may not interpret, rank, recommend" in text
    assert module.BEGIN in text


def test_generated_reference_excludes_friends_from_dispatch():
    rendered = module.render_organization_reference(
        ROOT / "workforce" / "organization.yaml"
    )
    assert "The workforce is proactive" in rendered
    assert "Amy (`amy`) | friend" in rendered
    assert "friends, not operational assignees" in rendered
