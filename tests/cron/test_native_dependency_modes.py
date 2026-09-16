"""Real Cron/agent/MCP dispatch with fixture-only provider and service transports."""

import asyncio
import json
import sqlite3
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import requests
from openai.types.chat import ChatCompletion

from cron import executions, jobs, scheduler
from hermes_constants import get_hermes_home
from run_agent import AIAgent
from tools import mcp_tool
from tools.registry import registry


NOTE = "mcp__fixture_notes__get_note"
TASKS = "mcp__fixture_tasks__get_tasks"


def _response(tool=None, *, tracked=False):
    final = "[SILENT]\n[WORKFLOW_STATUS:completed]" if tracked else "[SILENT]"
    message = {"role": "assistant", "content": final}
    if tool:
        message = {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": f"call_{tool}", "type": "function",
                            "function": {"name": tool, "arguments": "{}"}}],
        }
    return ChatCompletion.model_validate({
        "id": "fixture-response", "object": "chat.completion", "created": 1,
        "model": "fixture-model", "choices": [{"index": 0, "message": message,
        "finish_reason": "tool_calls" if tool else "stop"}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
    })


@pytest.mark.parametrize("branch,expected_status", [
    ("no_tools", "error"),
    ("preserve", "ok"),
    ("enrich", "ok"),
    ("failed_enrichment", "error"),
    ("failed_note", "error"),
])
@pytest.mark.parametrize("tracked", [False, True])
def test_native_agent_dependency_modes(monkeypatch, branch, expected_status, tracked):
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "model:\n  provider: openrouter\n  default: fixture-model\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture-key")
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", lambda **kwargs: {
        "provider": "openrouter", "api_mode": "chat_completions",
        "base_url": "https://example.invalid/v1", "api_key": "fixture-key",
    })
    network_attempts = []

    def deny_network(*args, **kwargs):
        network_attempts.append(str(args[-1]) if args else "unknown")
        raise AssertionError("Unexpected fixture network access")

    def metadata_transport(self, request, **kwargs):
        if str(request.url) == "https://example.invalid/api/show":
            return httpx.Response(404, json={"error": "Not an Ollama endpoint"}, request=request)
        return deny_network(str(request.url))

    def requests_metadata_transport(self, request, **kwargs):
        if request.url not in {
            "https://openrouter.ai/api/v1/models", "https://example.invalid/v1/models",
        } or request.method != "GET":
            return deny_network(request.url)
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({"data": [{"id": "fixture-model",
            "context_length": 128000, "pricing": {}}]}).encode()
        return response

    from agent import model_metadata

    monkeypatch.setattr(model_metadata, "_model_metadata_cache", {})
    monkeypatch.setattr(model_metadata, "_model_metadata_cache_time", 0)
    monkeypatch.setattr(requests.Session, "send", requests_metadata_transport)
    monkeypatch.setattr(httpx.Client, "send", metadata_transport)
    monkeypatch.setattr(httpx.AsyncClient, "send", deny_network)
    monkeypatch.setattr(socket.socket, "connect", deny_network)
    servers = {}
    registrations = {}
    for name in (NOTE, TASKS):
        _, server_name, tool_name = name.split("__")
        result = SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps({"fixture": name}))],
            isError=(name == TASKS and branch == "failed_enrichment")
                    or (name == NOTE and branch == "failed_note"),
            structuredContent=None, meta=None,
        )
        server = SimpleNamespace(session=SimpleNamespace(call_tool=AsyncMock(return_value=result)),
                                 _rpc_lock=None)
        servers[server_name] = server
        monkeypatch.setitem(mcp_tool._servers, server_name, server)
        registrations[name] = registry.snapshot_registration(name)
        registry.register(
            name=name, toolset=f"mcp-{server_name}",
            schema={"name": name, "description": "Read fixture data.",
                    "parameters": {"type": "object", "properties": {}}},
            handler=mcp_tool._make_tool_handler(server_name, tool_name, 10),
            check_fn=lambda: True,
        )

    def run_rpc(coro_or_factory, timeout=30):
        async def scoped():
            for server in servers.values():
                server._rpc_lock = asyncio.Lock()
            return await (coro_or_factory() if callable(coro_or_factory) else coro_or_factory)
        return asyncio.run(scoped())

    monkeypatch.setattr(mcp_tool, "_run_on_mcp_loop", run_rpc)
    calls = [] if branch == "no_tools" else [NOTE]
    if branch in {"enrich", "failed_enrichment"}:
        calls.append(TASKS)
    responses = iter([*map(_response, calls), _response(tracked=tracked)])
    model_requests = []

    def respond(self, api_kwargs):
        model_requests.append(api_kwargs)
        return next(responses)

    monkeypatch.setattr(AIAgent, "_interruptible_api_call", respond)
    try:
        job = jobs.create_job(
            prompt="Read the fixture note and preserve it or enrich it as needed.",
            schedule="every 1h", name="Native dependency fixture", deliver="local",
            model="fixture-model", provider="openrouter", max_iterations=5,
            enabled_toolsets=["mcp-fixture_notes", "mcp-fixture_tasks"],
            runtime_tool_budget={"max_calls": 3, "max_writes": 1,
                                "max_detail_reads": 3, "max_list_items": 10,
                                "allowed_tools": [NOTE, TASKS]},
            required_tool_dependencies=[NOTE, TASKS],
            required_tool_dependency_mode="when_invoked",
            required_tool_dependency_modes={NOTE: "always"},
            track_workflow_status=tracked,
        )
        assert scheduler.run_one_job(job) is True
        saved = next(item for item in jobs.list_jobs(include_disabled=True) if item["id"] == job["id"])
        assert saved["last_status"] == expected_status
        if tracked:
            assert saved["last_workflow_status"] == (
                "completed" if expected_status == "ok" else "failed"
            )
        assert len(model_requests) == len(calls) + 1
        assert not network_attempts
        for name in (NOTE, TASKS):
            server_name = name.split("__")[1]
            assert servers[server_name].session.call_tool.await_count == calls.count(name)
        outcome = saved["last_dependency_outcome"]
        assert set(outcome["missing"]) == {NOTE, TASKS} - set(calls)
        failed_names = ({TASKS} if branch == "failed_enrichment" else
                        {NOTE} if branch == "failed_note" else set())
        assert {item["tool"] for item in outcome["failed"]} == failed_names
        assert set(outcome["successful"]) == set(calls) - failed_names
        assert saved["last_dependency_status"] == (
            "healthy" if branch == "enrich" else
            "not_observed" if branch == "preserve" else "degraded"
        )
        history = executions.list_executions(job_id=job["id"])
        assert len(history) == 1
        assert history[0]["status"] == ("completed" if expected_status == "ok" else "failed")
        with sqlite3.connect(home / "state.db") as conn:
            sessions = conn.execute("SELECT id, end_reason FROM sessions").fetchall()
            assert len(sessions) == 1
            session_id, end_reason = sessions[0]
            assert session_id.startswith(f"cron_{job['id']}_")
            assert end_reason == "cron_complete"
            rows = conn.execute(
                "SELECT role, content, tool_name FROM messages WHERE session_id=? ORDER BY id",
                (session_id,),
            ).fetchall()
        assert any(role == "assistant" and "[SILENT]" in (content or "") for role, content, _ in rows)
        tool_rows = [(content, name) for role, content, name in rows if role == "tool"]
        assert [name for _, name in tool_rows] == calls
        for content, name in tool_rows:
            assert name in content
            assert ('"error"' in content) == (name in failed_names)
    finally:
        for name, snapshot in registrations.items():
            registry.restore_registration(name, registry.get_entry(name), snapshot)
        for server_name in servers:
            mcp_tool._server_error_counts.pop(server_name, None)
            mcp_tool._server_breaker_opened_at.pop(server_name, None)
