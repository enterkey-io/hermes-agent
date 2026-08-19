import json
from pathlib import Path

from scripts.workforce_compile import BEGIN, END
from scripts.workforce_preservation_report import build


def test_report_hashes_without_reproducing_private_content(tmp_path: Path):
    source = tmp_path / "source.md"
    candidate = tmp_path / "candidate.md"
    private = "private relationship wording must not enter the report\n"
    source.write_text(private)
    candidate.write_text(f"{BEGIN}\ncontract\n{END}\n" + private)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"profiles": [{"agent": "agent", "source": str(source), "candidate": str(candidate)}]}))
    report, markdown = build(manifest)
    assert report["valid"] is True
    assert private.strip() not in markdown
    assert report["results"][0]["unmanaged_content_byte_equivalent"] is True
