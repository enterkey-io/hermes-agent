import pytest

from scripts.workforce_behavior_validate import _has_provider, _parse_object


def test_parse_object_accepts_plain_or_fenced_json():
    expected = {"routine": "execute_verify_close"}
    assert _parse_object('{"routine":"execute_verify_close"}') == expected
    assert _parse_object('```json\n{"routine":"execute_verify_close"}\n```') == expected


def test_parse_object_rejects_non_object():
    with pytest.raises(ValueError):
        _parse_object('[]')


def test_has_provider_checks_provider_metadata_without_assuming_file_presence(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text('{"providers":{"openai-codex":{"token":"secret"}}}', encoding="utf-8")
    assert _has_provider(auth, "openai-codex") is True
    assert _has_provider(auth, "xai-oauth") is False
    assert _has_provider(tmp_path / "missing.json", "openai-codex") is False
