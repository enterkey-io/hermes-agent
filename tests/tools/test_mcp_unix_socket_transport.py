"""Real-path coverage for Streamable HTTP over a Unix-domain socket."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import socket
import subprocess
import sys
import textwrap
import time

import pytest

from tools import mcp_tool


def _config(path: Path, **overrides) -> dict:
    config = {
        "url": "http://localhost:8000/mcp",
        "unix_socket": str(path),
        "connect_timeout": 5,
        "timeout": 5,
    }
    config.update(overrides)
    return config


@pytest.mark.parametrize(
    "overrides,match",
    [
        ({"unix_socket": 42}, "absolute path"),
        ({"unix_socket": "relative.sock"}, "absolute path"),
        ({"url": "https://localhost/mcp"}, "plain http"),
        ({"url": "http://example.com/mcp"}, "plain http"),
        ({"url": "http://user@localhost/mcp"}, "without credentials"),
        ({"transport": "sse"}, "not SSE"),
        ({"auth": "oauth"}, "OAuth"),
        ({"client_cert": "/missing"}, "TLS client"),
        ({"ssl_verify": False}, "ssl_verify"),
    ],
)
def test_unix_socket_rejects_ambiguous_transport_combinations(
    tmp_path, overrides, match
):
    with pytest.raises(ValueError, match=match):
        mcp_tool._resolve_unix_socket(
            "private", _config(tmp_path / "mcp.sock", **overrides)
        )


def test_unix_socket_is_not_silently_ignored_for_stdio_server():
    task = mcp_tool.MCPServerTask("private")
    asyncio.run(
        task.run({
            "command": sys.executable,
            "args": ["-c", "raise SystemExit('must not start')"],
            "unix_socket": "/run/private/mcp.sock",
        })
    )

    assert isinstance(task._error, ValueError)
    assert str(task._error) == "MCP server 'private': unix_socket requires an HTTP url"


@pytest.mark.linux_only
def test_unix_socket_rejects_existing_non_socket_and_symlink(tmp_path):
    regular = tmp_path / "regular"
    regular.write_text("not a socket")
    with pytest.raises(ValueError, match="not a Unix socket"):
        mcp_tool._resolve_unix_socket("private", _config(regular))

    actual = tmp_path / "actual.sock"
    alias = tmp_path / "alias.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(actual))
        alias.symlink_to(actual)
        with pytest.raises(ValueError, match="not a Unix socket"):
            mcp_tool._resolve_unix_socket("private", _config(alias))


def test_missing_socket_is_valid_configuration_for_late_service_start(tmp_path):
    path = tmp_path / "missing.sock"
    assert mcp_tool._resolve_unix_socket("private", _config(path)) == str(path)


@pytest.mark.linux_only
def test_native_mcp_round_trip_uses_only_unix_socket(tmp_path):
    pytest.importorskip("mcp.server")
    pytest.importorskip("uvicorn")
    socket_path = tmp_path / "broker.sock"
    server_path = tmp_path / "server.py"
    server_path.write_text(
        textwrap.dedent(
            """
            import sys
            import uvicorn
            from mcp.server import MCPServer

            mcp = MCPServer("uds-test")

            @mcp.tool()
            def echo(value: str) -> str:
                return "uds:" + value

            app = mcp.streamable_http_app(
                streamable_http_path="/mcp",
                json_response=True,
                stateless_http=True,
                host="localhost",
            )
            uvicorn.run(
                app,
                uds=sys.argv[1],
                lifespan="on",
                log_level="error",
                proxy_headers=False,
            )
            """
        )
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(mcp_tool.__file__).resolve().parents[1])
    process = subprocess.Popen(
        [sys.executable, str(server_path), str(socket_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not socket_path.exists():
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                pytest.fail(f"test MCP server exited early: {stdout}\n{stderr}")
            time.sleep(0.02)
        assert socket_path.exists()

        async def exercise():
            server = await mcp_tool._connect_server("uds_test", _config(socket_path))
            try:
                assert [tool.name for tool in server._tools] == ["echo"]
                result = await server.session.call_tool(
                    "echo", arguments={"value": "ok"}
                )
                assert result.is_error is False
                assert [block.text for block in result.content] == ["uds:ok"]
            finally:
                await server.shutdown()

        asyncio.run(exercise())
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
