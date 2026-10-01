"""Runtime tools tested against a real local socket peer."""

import asyncio
import json
import os
import socket
import subprocess
import sys
from contextlib import asynccontextmanager, suppress
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from qlog_mcp import runtime
from qlog_mcp.server import create_server


@pytest.mark.skipif(sys.platform != "linux", reason="Flatpak endpoint is Linux-specific")
@pytest.mark.parametrize("runtime_directory", ["valid", "unset", "empty", "relative"])
def test_endpoint_uses_runtime_directory_without_temporary_fallback(tmp_path, runtime_directory):
    environment = {
        **os.environ, "TMPDIR": str(tmp_path), "TEMP": str(tmp_path), "TMP": str(tmp_path),
    }
    environment.pop("XDG_RUNTIME_DIR", None)
    if runtime_directory != "unset":
        environment["XDG_RUNTIME_DIR"] = {
            "valid": str(tmp_path), "empty": "", "relative": "relative",
        }[runtime_directory]
    result = subprocess.check_output(
        [
            sys.executable,
            "-c",
            "from qlog_mcp.runtime import RUNTIME_ENDPOINT; print(RUNTIME_ENDPOINT)",
        ],
        env=environment,
        text=True,
    )
    expected = None
    if runtime_directory == "valid":
        expected = tmp_path / "app/io.github.foldynl.QLog/qlog-runtime"
    elif runtime_directory == "unset":
        expected = f"/run/user/{os.getuid()}/app/io.github.foldynl.QLog/qlog-runtime"
    assert result.strip() == str(expected)


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("win32", r"\\.\pipe\qlog-runtime"),
        ("darwin", "Library/Application Support/io.github.foldynl.QLog/qlog-runtime"),
    ],
)
def test_platform_endpoint_does_not_depend_on_unix_temp_variables(
    tmp_path, monkeypatch, platform, expected
):
    monkeypatch.setattr(runtime, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(runtime.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "unrelated-temp"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "unrelated-runtime"))
    endpoint = runtime._runtime_endpoint()
    assert str(endpoint) == (expected if platform == "win32" else str(tmp_path / expected))


@asynccontextmanager
async def local_peer(tmp_path, monkeypatch, responses, transform=None):
    endpoint = r"\\.\pipe\qlog-test-" + uuid4().hex if sys.platform == "win32" else tmp_path / "runtime"
    monkeypatch.setattr("qlog_mcp.runtime.RUNTIME_ENDPOINT", endpoint)
    calls = []
    tasks = set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            while line := await reader.readline():
                request = json.loads(line)
                calls.append(request["method"])
                reply = {"id": request["id"], "result": responses[request["method"]]}
                if transform:
                    reply = transform(request, reply)
                if reply is None:
                    continue
                payload = reply if isinstance(reply, bytes) else json.dumps(reply).encode() + b"\n"
                split = len(payload) // 2
                writer.write(payload[:split])
                await writer.drain()
                await asyncio.sleep(0)
                writer.write(payload[split:])
                await writer.drain()
                if not payload.endswith(b"\n"):
                    return
        except ConnectionError:
            pass
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            tasks.discard(task)

    if sys.platform == "win32":
        def protocol_factory():
            return asyncio.StreamReaderProtocol(asyncio.StreamReader(), handle)

        servers = await asyncio.get_running_loop().start_serving_pipe(protocol_factory, endpoint)
    else:
        server = await asyncio.start_unix_server(handle, path=endpoint)
        endpoint.chmod(0o600)
        servers = [server]
    try:
        yield calls
    finally:
        for server in servers:
            server.close()
        if sys.platform != "win32":
            await server.wait_closed()
        for task in tuple(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.fixture
def responses():
    return {
        "system.get_info": {"protocol": 1, "application": "QLog", "version": "test"},
        "runtime.get_schema": {
            "fields": {
                "future_value": {
                    "type": "object",
                    "description": "Future provider",
                    "nullable": True,
                    "future_metadata": "Preserved",
                }
            }
        },
        "runtime.get_context": {
            "values": {"future_value": {"label": "Žluťoučký", "items": [1, None]}},
            "issues": {},
        },
    }


async def test_runtime_tool_surface_is_lazy():
    async with Client(create_server()) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        capabilities = await client.call_tool("qlog.get_capabilities")
    assert "qlog.get_live_context" in tools
    live = tools["qlog.get_live_context"]
    assert live.annotations.read_only_hint is True
    assert live.annotations.destructive_hint is False
    selection = live.input_schema["properties"]["sources"]
    assert selection["description"]
    array = next(item for item in selection["anyOf"] if item.get("type") == "array")
    assert array["minItems"] == 1
    assert array["items"]["minLength"] == 1
    catalog = tools["qlog.list_live_sources"]
    assert catalog.annotations.read_only_hint is True
    assert catalog.output_schema["properties"]["sources"]["description"]
    assert tools["qlog.get_schema"].input_schema["properties"]["sources"]["description"]
    for name in ("values", "issues"):
        assert live.output_schema["properties"][name]["description"]
    assert "runtime" in tools["qlog.get_schema"].input_schema["properties"]["domain"]["enum"]
    assert capabilities.data["runtime"] == {
        "get_live_context": True, "schema": True, "list_live_sources": True,
        "source_selection": True,
    }


async def test_live_context_and_schema_without_database(tmp_path, monkeypatch, responses):
    async with (
        local_peer(tmp_path, monkeypatch, responses) as calls,
        Client(create_server(tmp_path / "missing.db")) as client,
    ):
        live = await client.call_tool("qlog.get_live_context")
        schema = await client.call_tool("qlog.get_schema", {"domain": "runtime"})
        responses["runtime.get_context"]["values"]["future_value"] = "changed"
        fresh = await client.call_tool("qlog.get_live_context")
    assert live.structured_content == {
        "values": {"future_value": {"label": "Žluťoučký", "items": [1, None]}},
        "issues": {},
    }
    assert schema.data == {"runtime": responses["runtime.get_schema"]}
    assert fresh.structured_content["values"]["future_value"] == "changed"
    assert calls == [
        "system.get_info",
        "runtime.get_context",
        "system.get_info",
        "runtime.get_schema",
        "system.get_info",
        "runtime.get_context",
    ]
    assert not (tmp_path / "missing.db").exists()


@pytest.mark.parametrize("stale_socket", [False, True])
async def test_missing_runtime_does_not_break_database(
    qlog_database, tmp_path, monkeypatch, stale_socket
):
    endpoint = tmp_path / "missing"
    if sys.platform == "win32":
        if stale_socket:
            pytest.skip("Windows removes named pipes when their last handle closes")
        endpoint = r"\\.\pipe\qlog-missing-" + uuid4().hex
    monkeypatch.setattr("qlog_mcp.runtime.RUNTIME_ENDPOINT", endpoint)
    if stale_socket:
        with socket.socket(socket.AF_UNIX) as peer:
            peer.bind(str(endpoint))
        endpoint.chmod(0o600)
    async with Client(create_server(qlog_database)) as client:
        with pytest.raises(ToolError, match="QLog is not running"):
            await client.call_tool("qlog.get_live_context")
        context = await client.call_tool("qlog.get_context")
        schema = await client.call_tool("qlog.get_schema")
    assert context.data["qso_count"] == 4
    assert "callsign" in schema.data["qso"]["fields"]


async def test_missing_runtime_directory_keeps_database_tools_available(qlog_database, monkeypatch):
    monkeypatch.setattr("qlog_mcp.runtime.RUNTIME_ENDPOINT", None)
    async with Client(create_server(qlog_database)) as client:
        with pytest.raises(ToolError, match="desktop session"):
            await client.call_tool("qlog.get_live_context")
        context = await client.call_tool("qlog.get_context")
    assert context.data["qso_count"] == 4


@pytest.mark.skipif(sys.platform != "linux", reason="Flatpak endpoint is Linux-specific")
async def test_flatpak_intermediate_symlink_is_accepted(tmp_path, monkeypatch, responses):
    app_directory = tmp_path / "backing/io.github.foldynl.QLog"
    app_directory.mkdir(parents=True, mode=0o700)
    (tmp_path / "app").symlink_to(app_directory.parent, target_is_directory=True)
    async with local_peer(app_directory, monkeypatch, responses) as calls:
        monkeypatch.setattr(
            "qlog_mcp.runtime.RUNTIME_ENDPOINT",
            tmp_path / "app/io.github.foldynl.QLog/runtime",
        )
        async with Client(create_server()) as client:
            live = await client.call_tool("qlog.get_live_context")
    assert live.structured_content == responses["runtime.get_context"]
    assert calls == ["system.get_info", "runtime.get_context"]


async def test_windows_pipe_uses_same_protocol_and_closes_connection(tmp_path, monkeypatch, responses):
    loop = asyncio.get_running_loop()
    transports = []
    native_connect = getattr(loop, "create_pipe_connection", None)

    async def open_pipe(factory, endpoint):
        assert endpoint == r"\\.\pipe\qlog-runtime"
        if sys.platform == "win32":
            transport, protocol = await native_connect(factory, peer_endpoint)
        else:
            transport, protocol = await loop.create_unix_connection(factory, path=peer_endpoint)
        transports.append(transport)
        return transport, protocol

    async with local_peer(tmp_path, monkeypatch, responses) as calls:
        peer_endpoint = runtime.RUNTIME_ENDPOINT
        monkeypatch.setattr(runtime, "sys", SimpleNamespace(platform="win32"))
        monkeypatch.setattr(runtime, "RUNTIME_ENDPOINT", r"\\.\pipe\qlog-runtime")
        monkeypatch.setattr(loop, "create_pipe_connection", open_pipe, raising=False)
        async with Client(create_server()) as client:
            live = await client.call_tool("qlog.get_live_context")
    assert live.structured_content == responses["runtime.get_context"]
    assert calls == ["system.get_info", "runtime.get_context"]
    assert len(transports) == 1
    assert transports[0].is_closing()


async def test_windows_client_without_pipe_support_reports_connection_error(monkeypatch):
    monkeypatch.setattr(runtime, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(runtime, "RUNTIME_ENDPOINT", r"\\.\pipe\qlog-runtime")
    monkeypatch.setattr(asyncio.get_running_loop(), "create_pipe_connection", None, raising=False)
    async with Client(create_server()) as client:
        with pytest.raises(ToolError, match="Could not connect to QLog.*Windows"):
            await client.call_tool("qlog.get_live_context")


@pytest.mark.skipif(sys.platform == "win32", reason="Unix directory permissions")
async def test_macos_rejects_shared_directory_before_connecting(tmp_path, monkeypatch, responses):
    async with local_peer(tmp_path, monkeypatch, responses) as calls:
        monkeypatch.setattr(runtime, "sys", SimpleNamespace(platform="darwin"))
        tmp_path.chmod(0o755)
        async with Client(create_server()) as client:
            with pytest.raises(ToolError, match="securely connect"):
                await client.call_tool("qlog.get_live_context")
    assert calls == []


@pytest.mark.skipif(sys.platform != "linux", reason="Linux socket permissions")
@pytest.mark.parametrize(
    "problem", ["socket-permissions", "directory-permissions", "directory-link", "file", "owner"]
)
async def test_unsafe_endpoint_is_rejected_before_connecting(tmp_path, monkeypatch, responses, problem):
    async with local_peer(tmp_path, monkeypatch, responses) as calls:
        if problem == "socket-permissions":
            (tmp_path / "runtime").chmod(0o666)
        elif problem == "directory-permissions":
            tmp_path.chmod(0o755)
        elif problem == "directory-link":
            alias = tmp_path / "alias"
            alias.symlink_to(tmp_path, target_is_directory=True)
            monkeypatch.setattr("qlog_mcp.runtime.RUNTIME_ENDPOINT", alias / "runtime")
        elif problem == "file":
            endpoint = tmp_path / "runtime"
            endpoint.unlink()
            endpoint.write_text("ordinary file")
            endpoint.chmod(0o600)
        else:
            uid = os.getuid()
            monkeypatch.setattr("qlog_mcp.runtime.os.getuid", lambda: uid + 1)
        async with Client(create_server()) as client:
            with pytest.raises(ToolError, match="securely connect"):
                await client.call_tool("qlog.get_live_context")
        assert calls == []


@pytest.mark.parametrize("protocol", [2, True, "1", None])
async def test_incompatible_protocol_is_rejected(tmp_path, monkeypatch, responses, protocol):
    responses["system.get_info"]["protocol"] = protocol
    async with (
        local_peer(tmp_path, monkeypatch, responses) as calls,
        Client(create_server()) as client,
    ):
        with pytest.raises(ToolError, match="Unsupported QLog runtime protocol"):
            await client.call_tool("qlog.get_live_context")
    assert calls == ["system.get_info"]


@pytest.mark.parametrize(
    "bad_reply",
    [
        b"{invalid}\n",
        b"[]\n",
        b'{"id":"wrong","result":{}}\n',
        b'{"id":"info","result":{},"error":{}}\n',
        b'{"id":"info","result":[]}\n',
        b"",
        b'{"id":"info","result":{}}',
        b"x" * (1024 * 1024 + 1) + b"\n",
    ],
)
async def test_invalid_responses_are_rejected(tmp_path, monkeypatch, responses, bad_reply):
    async with (
        local_peer(tmp_path, monkeypatch, responses, lambda request, reply: bad_reply),
        Client(create_server()) as client,
    ):
        with pytest.raises(ToolError, match="Invalid QLog runtime response"):
            await client.call_tool("qlog.get_live_context")


async def test_remote_error_does_not_expose_peer_text(tmp_path, monkeypatch, responses):
    def error(request, reply):
        return {"id": request["id"], "error": {"code": "missing", "message": "secret-peer-text"}}

    async with (
        local_peer(tmp_path, monkeypatch, responses, error),
        Client(create_server()) as client,
    ):
        with pytest.raises(ToolError, match="QLog runtime rejected the request") as failure:
            await client.call_tool("qlog.get_live_context")
    assert "secret-peer-text" not in str(failure.value)


async def test_timeout_closes_connection(tmp_path, monkeypatch, responses):
    async with local_peer(tmp_path, monkeypatch, responses, lambda request, reply: None) as calls:
        monkeypatch.setattr("qlog_mcp.runtime.RUNTIME_TIMEOUT", 0.05)
        async with Client(create_server()) as client:
            with pytest.raises(ToolError, match="QLog did not respond in time"):
                await client.call_tool("qlog.get_live_context")
    assert calls == ["system.get_info"]


@pytest.mark.parametrize("error", [PermissionError("private-details"), NotImplementedError()])
async def test_other_connection_errors_do_not_claim_qlog_is_stopped(
    tmp_path, monkeypatch, responses, error
):
    async def fail_to_connect(*args, **kwargs):
        raise error

    monkeypatch.setattr(runtime.RuntimeClient, "_connect", staticmethod(fail_to_connect))
    async with local_peer(tmp_path, monkeypatch, responses), Client(create_server()) as client:
        with pytest.raises(ToolError, match="Could not connect to QLog") as failure:
            await client.call_tool("qlog.get_live_context")
    assert "QLog is not running" not in str(failure.value)
    assert "private-details" not in str(failure.value)


async def test_partial_context_is_returned(tmp_path, monkeypatch, responses):
    responses["runtime.get_context"] = {
        "values": {"good": 42, "slow": None},
        "issues": {"slow": "provider_timeout"},
    }
    async with local_peer(tmp_path, monkeypatch, responses), Client(create_server()) as client:
        live = await client.call_tool("qlog.get_live_context")
    assert live.structured_content == {
        "values": {"good": 42, "slow": None},
        "issues": {"slow": "provider_timeout"},
    }


@pytest.mark.parametrize(
    "result",
    [
        {"values": {}, "issues": []},
        {"values": [], "issues": {}},
        {"values": {}},
        {"values": {"bad": float("nan")}, "issues": {}},
    ],
)
async def test_invalid_context_shape(tmp_path, monkeypatch, responses, result):
    responses["runtime.get_context"] = result
    async with local_peer(tmp_path, monkeypatch, responses), Client(create_server()) as client:
        with pytest.raises(ToolError, match="Invalid QLog runtime"):
            await client.call_tool("qlog.get_live_context")


async def test_large_schema_is_not_limited_to_stream_default(tmp_path, monkeypatch, responses):
    responses["runtime.get_schema"]["fields"]["future_value"]["description"] = "x" * 70000
    async with local_peer(tmp_path, monkeypatch, responses), Client(create_server()) as client:
        result = await client.call_tool("qlog.get_schema", {"domain": "runtime"})
    assert len(result.data["runtime"]["fields"]["future_value"]["description"]) == 70000


@pytest.mark.parametrize("result", [{}, {"fields": []}, {"fields": {"bad": "number"}}])
async def test_invalid_schema_shape(tmp_path, monkeypatch, responses, result):
    responses["runtime.get_schema"] = result
    async with local_peer(tmp_path, monkeypatch, responses), Client(create_server()) as client:
        with pytest.raises(ToolError, match="Invalid QLog runtime schema"):
            await client.call_tool("qlog.get_schema", {"domain": "runtime"})


@pytest.mark.parametrize("field", ["type", "description", "nullable"])
async def test_schema_requires_field_metadata(tmp_path, monkeypatch, responses, field):
    del responses["runtime.get_schema"]["fields"]["future_value"][field]
    async with local_peer(tmp_path, monkeypatch, responses), Client(create_server()) as client:
        with pytest.raises(ToolError, match="Invalid QLog runtime schema"):
            await client.call_tool("qlog.get_schema", {"domain": "runtime"})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("type", 17),
        ("description", []),
        ("nullable", "yes"),
        ("nullable", 1),
        ("unit", None),
        ("unit", []),
        ("source", 17),
    ],
)
async def test_schema_rejects_invalid_metadata_types(tmp_path, monkeypatch, responses, field, value):
    responses["runtime.get_schema"]["fields"]["future_value"][field] = value
    async with local_peer(tmp_path, monkeypatch, responses), Client(create_server()) as client:
        with pytest.raises(ToolError, match="Invalid QLog runtime schema"):
            await client.call_tool("qlog.get_schema", {"domain": "runtime"})


async def test_schema_preserves_units_and_additional_metadata(tmp_path, monkeypatch, responses):
    metadata = responses["runtime.get_schema"]["fields"]["future_value"]
    metadata["nullable"] = False
    metadata["unit"] = "custom-unit"
    async with local_peer(tmp_path, monkeypatch, responses), Client(create_server()) as client:
        result = await client.call_tool("qlog.get_schema", {"domain": "runtime"})
    assert result.data == {"runtime": responses["runtime.get_schema"]}


async def test_usage_log_omits_runtime_values(tmp_path, monkeypatch, responses):
    usage_log = tmp_path / "usage.jsonl"
    async with (
        local_peer(tmp_path, monkeypatch, responses),
        Client(create_server(usage_log_path=usage_log)) as client,
    ):
        await client.call_tool("qlog.get_live_context")
        await client.call_tool("qlog.get_schema", {"domain": "runtime"})
    raw = usage_log.read_text()
    assert "Žluťoučký" not in raw
    assert "future_value" not in raw
    assert "Preserved" not in raw
    events = [json.loads(line) for line in raw.splitlines()]
    assert events[1]["arguments"] == {"domain": "runtime"}


async def test_source_catalog_is_forwarded_without_reading_values(tmp_path, monkeypatch, responses):
    responses['runtime.list_sources'] = {
        'sources': {'future': {'description': 'Current temperature and sensor connection state.'}}
    }
    async with (
        local_peer(tmp_path, monkeypatch, responses) as calls,
        Client(create_server(tmp_path / 'missing.db')) as client,
    ):
        result = await client.call_tool('qlog.list_live_sources')
    assert result.structured_content == {
        'sources': {'future': {'description': 'Current temperature and sensor connection state.'}}
    }
    assert calls == ['system.get_info', 'runtime.list_sources']
    assert not (tmp_path / 'missing.db').exists()


async def test_source_selection_is_sent_to_qlog(tmp_path, monkeypatch, responses):
    received = []

    def capture(request, reply):
        received.append(request)
        return reply

    async with (
        local_peer(tmp_path, monkeypatch, responses, capture),
        Client(create_server()) as client,
    ):
        await client.call_tool('qlog.get_live_context', {'sources': ['future', 'sensor']})
        await client.call_tool('qlog.get_schema', {'domain': 'runtime', 'sources': ['sensor']})
    assert received == [
        {'id': 'info', 'method': 'system.get_info'},
        {'id': 'request', 'method': 'runtime.get_context', 'params': {'sources': ['future', 'sensor']}},
        {'id': 'info', 'method': 'system.get_info'},
        {'id': 'request', 'method': 'runtime.get_schema', 'params': {'sources': ['sensor']}},
    ]


@pytest.mark.parametrize("tool, arguments", [
    ("qlog.get_live_context", {"sources": []}),
    ("qlog.get_live_context", {"sources": [17]}),
    ("qlog.get_live_context", {"sources": [""]}),
    ("qlog.get_live_context", {"sources": "rig"}),
    ("qlog.get_schema", {"domain": "runtime", "sources": []}),
    ("qlog.get_schema", {"domain": "qso", "sources": ["rig"]}),
    ("qlog.get_schema", {"domain": "catalog", "sources": ["rig"]}),
])
async def test_invalid_source_selection_never_contacts_qlog(tmp_path, monkeypatch, responses,
                                                          tool, arguments):
    async with (
        local_peer(tmp_path, monkeypatch, responses) as calls,
        Client(create_server()) as client,
    ):
        with pytest.raises(ToolError):
            await client.call_tool(tool, arguments)
    assert calls == []


@pytest.mark.parametrize("result", [
    {}, {"sources": []}, {"sources": {"rig": {}}},
    {"sources": {"rig": {"description": 17}}},
    {"sources": {"rig": {"description": ""}}},
    {"sources": {"": {"description": "State"}}},
])
async def test_invalid_source_metadata_is_rejected(tmp_path, monkeypatch, responses, result):
    responses["runtime.list_sources"] = result
    async with local_peer(tmp_path, monkeypatch, responses), Client(create_server()) as client:
        with pytest.raises(ToolError, match="Invalid QLog runtime sources"):
            await client.call_tool("qlog.list_live_sources")


async def test_rejected_source_selection_requests_metadata_refresh(tmp_path, monkeypatch, responses):
    def reject(request, reply):
        if request["method"] == "runtime.get_context":
            return {"id": request["id"], "error": {
                "code": "invalid_params", "message": "private-peer-details"
            }}
        return reply

    async with (
        local_peer(tmp_path, monkeypatch, responses, reject),
        Client(create_server()) as client,
    ):
        with pytest.raises(ToolError, match="source selection") as failure:
            await client.call_tool("qlog.get_live_context", {"sources": ["removed"]})
    assert "qlog.list_live_sources" in str(failure.value)
    assert "private-peer-details" not in str(failure.value)
