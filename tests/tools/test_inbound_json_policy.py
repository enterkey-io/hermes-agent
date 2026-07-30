from __future__ import annotations

import json
from pathlib import Path

from agent.execution_capabilities import (
    _bind_execution_context,
    _issue_cron_job_execution_context,
    _issue_tool_invocation_grant,
)
from cron import scheduler
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from tools.registry import ToolRegistry


JOB_ID = "8ff7fa2ddb8b"
TOOL_NAME = "_test_strict_inbound_json"


def _schema() -> dict:
    return {
        "name": TOOL_NAME,
        "description": "strict inbound JSON",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "pattern": "^[^\\u0000-\\u001f\\u007f-\\u009f]*$",
                },
                "nested": {
                    "type": "object",
                    "properties": {
                        "summary": {
                            "type": "string",
                            "pattern": (
                                "^[^\\u0000-\\u0008\\u000b-"
                                "\\u001f\\u007f-\\u009f]*$"
                            ),
                        }
                    },
                },
            },
        },
    }


def _registered(tmp_path: Path):
    tool_registry = ToolRegistry()
    context = PluginContext(
        PluginManifest(
            name="Emily Paperclip Job",
            key="emily-paperclip-job",
            source="user",
        ),
        PluginManager(),
    )
    context.require_inbound_json_policy(reject_duplicate_object_keys=True)
    requirement = context.cron_job_capability(
        profile_name="emily",
        hermes_home=tmp_path,
        job_id=JOB_ID,
    )
    calls = []
    context._register_tool_in(
        tool_registry,
        name=TOOL_NAME,
        toolset="test",
        schema=_schema(),
        handler=lambda args, **kwargs: calls.append(args) or '{"ok":true}',
        execution_capability=requirement,
    )
    job = {"id": JOB_ID}
    permit = scheduler._issue_registered_cron_dispatch_from_registry(
        tool_registry,
        job,
        profile_name="emily",
        hermes_home=tmp_path,
        execution_id="execution-json",
    )
    execution_context = _issue_cron_job_execution_context(
        permit=permit,
        job=job,
        execution_id="execution-json",
        profile_name="emily",
        hermes_home=tmp_path,
        requirement=requirement,
    )
    owner = object()
    _bind_execution_context(execution_context, owner)
    return tool_registry, execution_context, owner, calls


def _dispatch_raw(tool_registry, context, owner, raw):
    args, error, admission = tool_registry.parse_inbound_json_arguments(
        TOOL_NAME,
        raw,
        execution_context=context,
        execution_owner=owner,
    )
    if error is not None:
        return json.loads(error)
    grant = _issue_tool_invocation_grant(
        context,
        owner=owner,
        tool_name=TOOL_NAME,
    )
    return json.loads(
        tool_registry.dispatch(
            TOOL_NAME,
            args,
            _execution_capability_grant=grant,
            _execution_capability_owner=owner,
            _execution_context=context,
            _inbound_json_admission=admission,
        )
    )


def test_recursive_duplicate_keys_fail_before_handler(tmp_path):
    tool_registry, context, owner, calls = _registered(tmp_path)

    result = _dispatch_raw(
        tool_registry,
        context,
        owner,
        '{"title":"ok","nested":{"summary":"first","summary":"second"}}',
    )

    assert result["error_type"] == "inbound_json_policy"
    assert calls == []


def test_configured_c0_c1_patterns_fail_before_handler_but_allow_lf_tab(
    tmp_path,
):
    tool_registry, context, owner, calls = _registered(tmp_path)

    control = _dispatch_raw(
        tool_registry,
        context,
        owner,
        '{"title":"bad\\u001b","nested":{"summary":"ok"}}',
    )
    allowed = _dispatch_raw(
        tool_registry,
        context,
        owner,
        '{"title":"ok","nested":{"summary":"line 1\\n\\tline 2"}}',
    )

    assert control["error_type"] == "inbound_json_policy"
    assert allowed == {"ok": True}
    assert calls == [
        {"title": "ok", "nested": {"summary": "line 1\n\tline 2"}}
    ]


def test_protected_policy_handler_requires_one_use_raw_parse_admission(tmp_path):
    tool_registry, context, owner, calls = _registered(tmp_path)
    grant = _issue_tool_invocation_grant(
        context,
        owner=owner,
        tool_name=TOOL_NAME,
    )

    missing = json.loads(
        tool_registry.dispatch(
            TOOL_NAME,
            {"title": "ok", "nested": {"summary": "ok"}},
            _execution_capability_grant=grant,
            _execution_capability_owner=owner,
            _execution_context=context,
        )
    )

    assert missing["error_type"] == "inbound_json_policy_unavailable"
    assert calls == []
