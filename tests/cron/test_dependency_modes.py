"""Operator-owned mixed dependency policy validation and persistence."""

import pytest

from cron import jobs
from tools.required_dependency_runtime import blocking_missing_dependencies


NOTE = "mcp__evernote__get_note"
TASKS = "mcp__nirvana__get_tasks"


def create(**kwargs):
    return jobs.create_job(prompt="Verify note", schedule="every 1h",
                           required_tool_dependencies=[NOTE, TASKS], **kwargs)


@pytest.mark.parametrize("modes", [
    [], "always", False, {"unknown": "always"}, {NOTE: None},
    {NOTE: True}, {NOTE: "sometimes"}, {" " + NOTE: "always"},
])
def test_invalid_overrides_do_not_create_or_update_job(tmp_path, modes):
    with jobs.use_cron_store(tmp_path):
        with pytest.raises(ValueError, match="required_tool_dependency_modes"):
            create(required_tool_dependency_modes=modes)
        assert jobs.list_jobs(include_disabled=True) == []
        original = create()
        with pytest.raises(ValueError, match="required_tool_dependency_modes"):
            jobs.update_job(original["id"], {"required_tool_dependency_modes": modes})
        assert jobs.get_job(original["id"]) == original


def test_overrides_persist_and_validate_the_merged_dependency_set(tmp_path):
    with jobs.use_cron_store(tmp_path):
        modes = {NOTE: "always"}
        original = create(required_tool_dependency_mode="when_invoked",
                          required_tool_dependency_modes=modes)
        modes[NOTE] = "when_invoked"
        stored = jobs.get_job(original["id"])
        assert stored["required_tool_dependency_modes"] == {NOTE: "always"}
        renamed = jobs.update_job(original["id"], {"name": "Renamed"})
        assert renamed["required_tool_dependency_modes"] == {NOTE: "always"}
        with pytest.raises(ValueError, match="declared dependencies"):
            jobs.update_job(original["id"], {"required_tool_dependencies": [TASKS]})
        assert jobs.get_job(original["id"]) == renamed
        cleared = jobs.update_job(original["id"], {
            "required_tool_dependencies": [TASKS], "required_tool_dependency_modes": {},
        })
        assert cleared["required_tool_dependencies"] == [TASKS]
        assert cleared["required_tool_dependency_modes"] == {}


def test_overrides_cannot_exist_without_declared_dependencies(tmp_path):
    with jobs.use_cron_store(tmp_path):
        with pytest.raises(ValueError, match="declared dependencies"):
            jobs.create_job(prompt="Check", schedule="every 1h",
                            required_tool_dependency_modes={NOTE: "always"})


@pytest.mark.parametrize("default,modes,expected", [
    (None, None, [NOTE, TASKS]),
    ("when_invoked", None, []),
    ("when_invoked", {NOTE: "always"}, [NOTE]),
    ("always", {TASKS: "when_invoked"}, [NOTE]),
    ("when_invoked", {NOTE: None}, [NOTE]),
    ("when_invoked", [], [NOTE, TASKS]),
])
def test_blocking_missing_modes_and_legacy_defaults(default, modes, expected):
    assert blocking_missing_dependencies([NOTE, TASKS], mode=default, modes=modes) == expected
