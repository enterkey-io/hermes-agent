"""Behavioral contract for the shared Hermes agent-photo skill."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest


SKILL_DIR = Path(__file__).parents[2] / "skills" / "media" / "agent-photo"
SCRIPTS_DIR = SKILL_DIR / "scripts"
AUTHORIZED_WRAPPER = "$HOME/.local/bin/hermes-agent-photo"


def _load_module(name: str, filename: str):
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(SCRIPTS_DIR))


@pytest.fixture(scope="module")
def identity_parser():
    return _load_module("shared_agent_photo_identity_parser", "identity_parser.py")


@pytest.fixture(scope="module")
def generate():
    return _load_module("shared_agent_photo_generate", "generate.py")


@pytest.fixture(scope="module")
def prompt_profiles():
    return _load_module("shared_agent_photo_prompt_profiles", "prompt_profiles.py")


def test_skill_instructions_require_authorized_wrapper_without_direct_bypass():
    instructions = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    normalized = instructions.lower()

    assert AUTHORIZED_WRAPPER in instructions
    assert "uv " not in normalized
    assert "generate.py" not in normalized


def test_generator_reports_no_op_fallback_wrapper_contract(
    monkeypatch, capsys, generate
):
    monkeypatch.setattr(
        generate,
        "resolve_profile_dir",
        lambda: pytest.fail("contract probe must not inspect a profile"),
    )
    monkeypatch.setattr(
        generate,
        "load_workspace_agent",
        lambda: pytest.fail("contract probe must not load identity"),
    )
    for name in ("GEMINI_API_KEY", "NOVITA_API_KEY", "XAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    result = generate.main(["--wrapper-contract", "compatibility-probe"])

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    expected_files = {
        "SKILL.md",
        "requirements.txt",
        "references/photo-prompting-rules.md",
        "scripts/generate.py",
        "scripts/identity_parser.py",
        "scripts/prompt_profiles.py",
    }
    assert payload["contract"] == "hermes-agent-photo/no-op-fallback/v2"
    assert set(payload["files"]) == expected_files
    assert set(payload["dependencies"]) == {"Pillow", "requests"}
    for relative in expected_files:
        assert payload["files"][relative] == hashlib.sha256(
            (SKILL_DIR / relative).read_bytes()
        ).hexdigest()


def test_credential_probe_reports_only_injected_provider_names(
    monkeypatch, capsys, generate
):
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini")
    monkeypatch.setenv("NOVITA_API_KEY", "test-novita")
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setattr(
        generate.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail(
            "credential probe must not invoke 1Password or a provider"
        ),
    )

    result = generate.main(
        [
            "--credential-probe",
            "gemini",
            "--credential-probe",
            "novita",
        ]
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out) == {
        "availableNames": ["GEMINI_API_KEY", "NOVITA_API_KEY"],
        "missingNames": [],
        "status": "pass",
    }


def test_profile_root_prefers_hermes_home(monkeypatch, tmp_path, identity_parser):
    profile = tmp_path / "profiles" / "grace"
    profile.mkdir(parents=True)
    (profile / "identity.md").write_text("# Grace\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.chdir(tmp_path)

    assert identity_parser.resolve_profile_dir() == profile.resolve()


def test_curated_assets_seed_wins_over_root_copy(tmp_path, identity_parser):
    profile = tmp_path / "grace"
    assets = profile / "assets"
    assets.mkdir(parents=True)
    root_seed = profile / "lifelike-seed.png"
    curated_seed = assets / "lifelike-seed.jpg"
    root_seed.write_bytes(b"root")
    curated_seed.write_bytes(b"curated")

    assert identity_parser.find_seed_image(profile) == curated_seed


def test_preview_does_not_require_paid_approval(monkeypatch, capsys, generate, tmp_path):
    seed = tmp_path / "seed.png"
    seed.write_bytes(b"seed")
    monkeypatch.setattr(
        generate,
        "load_workspace_agent",
        lambda: ({"name": "Grace"}, seed, "grace"),
    )
    monkeypatch.setattr(generate, "build_prompt", lambda **_: "BUILT PROMPT")

    result = generate.main(["--preview-prompt", "at a desk"])

    assert result == 0
    assert "BUILT PROMPT" in capsys.readouterr().out


def test_authorized_no_op_fallback_refuses_missing_key_without_running_op(
    monkeypatch, generate
):
    attempted_commands = []
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    def unexpected_subprocess(command, **_kwargs):
        attempted_commands.append(command)
        raise AssertionError("authorized generation must not invoke op")

    monkeypatch.setattr(generate.subprocess, "run", unexpected_subprocess)

    with pytest.raises(RuntimeError, match="authorized credential.*not injected"):
        generate._get_api_key("gemini", allow_op_fallback=False)

    assert attempted_commands == []


def test_standalone_key_lookup_keeps_1password_fallback(monkeypatch, generate):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    class Result:
        returncode = 0
        stdout = "standalone-key\n"

    monkeypatch.setattr(generate.subprocess, "run", lambda *_args, **_kwargs: Result())

    assert generate._get_api_key("gemini") == "standalone-key"


def test_no_op_fallback_cli_mode_never_runs_op(monkeypatch, generate, tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    seed = profile / "seed.png"
    seed.write_bytes(b"seed")
    attempted_commands = []
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(generate, "resolve_profile_dir", lambda: profile)
    monkeypatch.setattr(
        generate,
        "load_workspace_agent",
        lambda: ({"name": "Test Agent"}, seed, "test-agent"),
    )
    monkeypatch.setattr(generate, "build_prompt", lambda **_kwargs: "prompt")
    monkeypatch.setattr(
        generate.subprocess,
        "run",
        lambda command, **_kwargs: attempted_commands.append(command),
    )

    def provider(**_kwargs):
        return [generate._get_api_key("gemini")]

    result = generate.main(
        ["--approved", "--no-op-fallback", "at a desk"],
        providers={"gemini": provider},
    )

    assert result == 1
    assert attempted_commands == []


def test_paid_generation_refuses_without_current_request_approval(generate, capsys):
    called = False

    def provider(**_kwargs):
        nonlocal called
        called = True
        return []

    result = generate.main(["at a desk"], providers={"gemini": provider})

    assert result == 2
    assert called is False
    assert "--approved" in capsys.readouterr().err


def test_selected_provider_is_the_only_default_attempt(generate):
    assert generate.provider_sequence("gemini", allow_fallback=False) == ["gemini"]
    assert generate.provider_sequence("grok", allow_fallback=False) == ["grok"]


def test_fallback_requires_explicit_opt_in(generate):
    assert generate.provider_sequence("gemini", allow_fallback=True) == [
        "gemini",
        "grok",
        "seedream",
    ]


def test_media_mirroring_emits_one_line_per_image(generate, tmp_path):
    profile = tmp_path / "grace"
    photos = profile / "media" / "photos"
    photos.mkdir(parents=True)
    first = photos / "one.png"
    second = photos / "two.png"
    first.write_bytes(b"one")
    second.write_bytes(b"two")

    mirrored = generate.mirror_to_media([first, second], profile)
    lines = generate.media_lines(mirrored)

    assert [path.parent for path in mirrored] == [profile / "media"] * 2
    assert lines == [
        f"MEDIA: {profile / 'media' / 'one.png'}",
        f"MEDIA: {profile / 'media' / 'two.png'}",
    ]
    assert len(lines) == len(set(lines)) == 2


def test_media_mirroring_preserves_duplicate_basenames(generate, tmp_path):
    profile = tmp_path / "grace"
    first = tmp_path / "provider-a" / "same.png"
    second = tmp_path / "provider-b" / "same.png"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(b"one")
    second.write_bytes(b"two")

    mirrored = generate.mirror_to_media([first, second], profile)

    assert [path.name for path in mirrored] == ["same.png", "same-2.png"]
    assert [path.read_bytes() for path in mirrored] == [b"one", b"two"]


def test_explicit_sources_must_be_regular_images(generate, tmp_path):
    profile = tmp_path / "grace"
    profile.mkdir()
    valid = profile / "baseline.jpg"
    invalid = profile / "notes.txt"
    valid.write_bytes(b"image")
    invalid.write_text("not an image", encoding="utf-8")

    assert generate.validate_sources([str(valid)]) == [valid.resolve()]
    with pytest.raises(ValueError, match="supported image"):
        generate.validate_sources([str(invalid)])


def test_sources_cannot_cross_profile_boundary(generate, tmp_path):
    profile = tmp_path / "grace"
    profile.mkdir()
    outside = tmp_path / "xenia-seed.jpg"
    outside.write_bytes(b"image")

    with pytest.raises(ValueError, match="active Hermes profile"):
        generate.validate_sources([str(outside)], profile_dir=profile)


def test_prompt_builder_accepts_resolved_path_sources(prompt_profiles, tmp_path):
    source = tmp_path / "baseline.jpg"

    line = prompt_profiles._source_reference_line([source])

    assert str(source) in line


def test_data_url_uses_the_bytes_actual_mime(monkeypatch, generate, tmp_path):
    source = tmp_path / "seed.png"
    source.write_bytes(b"png")
    monkeypatch.setattr(generate, "compress_image_if_needed", lambda *_args, **_kwargs: b"encoded")

    uncompressed = generate.image_data_url(source, max_size_mb=1)
    compressed = generate.image_data_url(source, max_size_mb=0)

    assert uncompressed.startswith("data:image/png;base64,")
    assert compressed.startswith("data:image/jpeg;base64,")


def test_saved_prompt_must_stay_inside_profile(monkeypatch, generate, tmp_path):
    profile = tmp_path / "grace"
    profile.mkdir()
    seed = profile / "seed.png"
    seed.write_bytes(b"seed")
    outside = tmp_path / "outside.txt"
    monkeypatch.setattr(generate, "resolve_profile_dir", lambda: profile)
    monkeypatch.setattr(
        generate,
        "load_workspace_agent",
        lambda: ({"name": "Grace"}, seed, "grace"),
    )
    monkeypatch.setattr(generate, "build_prompt", lambda **_: "PRIVATE PROMPT")

    result = generate.main(
        ["--preview-prompt", "--save-prompt", str(outside), "at a desk"]
    )

    assert result == 1
    assert not outside.exists()


class _Response:
    def __init__(self, payload=None, content=b"", status_code=200):
        self._payload = payload or {}
        self.content = content
        self.status_code = status_code
        self.text = "provider error"

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_gemini_uses_header_auth_and_redacts_errors(monkeypatch, capsys, generate, tmp_path):
    captured = {}
    secret = "gemini-secret-value"
    seed = tmp_path / "seed.png"
    output = tmp_path / "out.png"
    seed.write_bytes(b"seed")
    monkeypatch.setattr(generate, "image_bytes_and_mime", lambda *_args, **_kwargs: (b"seed", "image/png"))
    monkeypatch.setattr(generate, "_get_api_key", lambda _provider: secret)

    def fake_post(url, **kwargs):
        captured.update(url=url, kwargs=kwargs)
        return _Response(status_code=400)

    monkeypatch.setattr(generate.requests, "post", fake_post)

    assert generate.generate_photo_gemini("prompt", seed, output) == []
    assert secret not in captured["url"]
    assert captured["kwargs"]["headers"]["x-goog-api-key"] == secret
    assert secret not in capsys.readouterr().out


def test_grok_request_and_response_contract(monkeypatch, generate, tmp_path):
    captured = {}
    seed = tmp_path / "seed.jpg"
    output = tmp_path / "out.png"
    seed.write_bytes(b"seed")
    monkeypatch.setattr(generate, "image_data_url", lambda *_args, **_kwargs: "data:image/jpeg;base64,c2VlZA==")
    monkeypatch.setattr(generate, "_get_api_key", lambda _provider: "xai-secret")

    def fake_post(url, **kwargs):
        captured.update(url=url, kwargs=kwargs)
        payload = base64.b64encode(b"grok-image").decode("ascii")
        return _Response({"data": [{"b64_json": payload}]})

    monkeypatch.setattr(generate.requests, "post", fake_post)

    assert generate.generate_photo("prompt", seed, output) == [output]
    assert captured["url"] == "https://api.x.ai/v1/images/edits"
    assert captured["kwargs"]["json"]["model"] == "grok-imagine-image-pro"
    assert len(captured["kwargs"]["json"]["image"]) == 2
    assert output.read_bytes() == b"grok-image"


def test_seedream_request_and_response_contract(monkeypatch, generate, tmp_path):
    captured = {}
    seed = tmp_path / "seed.jpg"
    output = tmp_path / "out.png"
    seed.write_bytes(b"seed")
    monkeypatch.setattr(generate, "image_data_url", lambda *_args, **_kwargs: "data:image/jpeg;base64,c2VlZA==")
    monkeypatch.setattr(generate, "_get_api_key", lambda _provider: "novita-secret")

    def fake_post(url, **kwargs):
        captured.update(url=url, kwargs=kwargs)
        return _Response({"images": ["https://images.example/result.png"]})

    monkeypatch.setattr(generate.requests, "post", fake_post)
    monkeypatch.setattr(generate.requests, "get", lambda *_args, **_kwargs: _Response(content=b"seedream-image"))

    assert generate.generate_photo_seedream("prompt", seed, output) == [output]
    assert captured["url"] == "https://api.novita.ai/v3/seedream-4.5"
    assert captured["kwargs"]["json"]["image"] == ["data:image/jpeg;base64,c2VlZA=="]
    assert output.read_bytes() == b"seedream-image"
