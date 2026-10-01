"""Read live QLog state through its fixed local runtime endpoint."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from contextlib import suppress
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from .errors import InvalidQueryError, RuntimeProtocolError, RuntimeUnavailableError


def _runtime_endpoint() -> Path | str | None:
    """Match QLog's QLocalServer endpoint without discovery or filesystem writes."""
    if sys.platform == "win32":
        return r"\\.\pipe\qlog-runtime"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/io.github.foldynl.QLog/qlog-runtime"
    if sys.platform == "linux":
        # Flatpak shares this per-app directory with the host; do not use a /tmp fallback.
        directory = os.environ.get("XDG_RUNTIME_DIR")
        if directory is None:
            # Some MCP hosts omit desktop variables; this is a fixed per-user path, not discovery.
            directory = f"/run/user/{os.getuid()}"
        return (
            Path(directory) / "app/io.github.foldynl.QLog/qlog-runtime"
            if directory and Path(directory).is_absolute()
            else None
        )
    return Path(os.environ.get("TMPDIR") or "/tmp") / "qlog-runtime"


RUNTIME_ENDPOINT = _runtime_endpoint()
RUNTIME_TIMEOUT = 2.0
MAX_RESPONSE_BYTES = 1024 * 1024


SourceName = Annotated[str, Field(strict=True, min_length=1)]
SourceSelection = Annotated[
    list[SourceName] | None,
    Field(
        min_length=1,
        description=(
            "Names of live data sources advertised by qlog.list_live_sources. Select only "
            "sources relevant to the question, for example ['rig'] for radio connection, "
            "main VFO frequency, amateur band or operating mode. QLog reads only these "
            "providers, each in its owning thread. Omitted or null selects all sources for "
            "backward compatibility; an empty list is invalid. Names are supplied by QLog, "
            "not a fixed MCP enum. This selection applies only to domain='runtime' for schema."
        ),
    ),
]


class LiveSource(BaseModel):
    """QLog-owned metadata used to choose a provider before reading its fields."""

    model_config = ConfigDict(extra="allow", strict=True)

    description: str = Field(
        min_length=1,
        description=(
            "English explanation supplied by QLog of the state this source exposes, the "
            "questions it can answer, units and availability, and relevant limitations. "
            "Use it to choose a source; get its field schema for exact value semantics."
        ),
    )


class LiveSources(BaseModel):
    """Available live sources, without collecting their current values."""

    model_config = ConfigDict(extra="forbid", strict=True)

    sources: dict[SourceName, LiveSource] = Field(
        description=(
            "Map of registered source names to their QLog-owned descriptions. Use these "
            "exact names in sources on qlog.get_schema(domain='runtime') and "
            "qlog.get_live_context. A listed source is available for requests, not necessarily "
            "connected or responsive. This is metadata, not current hardware state."
        )
    )


class LiveContext(BaseModel):
    """Provider values and collection issues returned by QLog."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    values: dict[str, JsonValue] = Field(
        description=(
            "Map of field names from the requested sources to the latest state held by QLog. Use "
            "qlog.get_schema(domain='runtime') for each field's meaning, type and unit. "
            "Null means unavailable or unknown, not zero or false; check issues for collection "
            "failures. Each provider supplies one coherent snapshot, but different providers "
            "are not sampled atomically. These values are not historical QSO records."
        )
    )
    issues: dict[str, str] = Field(
        description=(
            "Map of affected runtime field names to collection failure codes: provider_timeout "
            "means no reply before QLog's deadline; provider_busy means an earlier request is "
            "still pending; provider_unavailable means the provider disappeared during "
            "collection; context_busy means another collection is active. Affected values are "
            "null; retain successful values from other providers. An empty map means no "
            "collection failures, but null values can still be unknown or inapplicable."
        )
    )


class RuntimeClient:
    """Open one connection per request; construction never contacts QLog."""

    async def sources(self) -> LiveSources:
        result = await self._request("runtime.list_sources")
        try:
            return LiveSources.model_validate(result)
        except ValidationError:
            raise RuntimeProtocolError("Invalid QLog runtime sources") from None

    async def context(self, sources: SourceSelection = None) -> LiveContext:
        result = await self._request("runtime.get_context", sources)
        try:
            return LiveContext.model_validate(result)
        except ValidationError:
            raise RuntimeProtocolError("Invalid QLog runtime context") from None

    async def schema(self, sources: SourceSelection = None) -> dict[str, Any]:
        result = await self._request("runtime.get_schema", sources)
        fields = result.get("fields")
        if not isinstance(fields, dict) or not all(
            isinstance(metadata, dict)
            and isinstance(metadata.get("type"), str)
            and isinstance(metadata.get("description"), str)
            and isinstance(metadata.get("nullable"), bool)
            and ("unit" not in metadata or isinstance(metadata["unit"], str))
            and ("source" not in metadata or isinstance(metadata["source"], str))
            for metadata in fields.values()
        ):
            raise RuntimeProtocolError("Invalid QLog runtime schema")
        return result

    async def _request(self, method: str, sources: SourceSelection = None) -> dict[str, Any]:
        writer: asyncio.StreamWriter | None = None
        try:
            if RUNTIME_ENDPOINT is None:
                raise RuntimeUnavailableError("Could not connect to QLog in this desktop session.")
            if sys.platform in {"linux", "darwin"}:
                directory_info = RUNTIME_ENDPOINT.parent.lstat()
                socket_info = RUNTIME_ENDPOINT.lstat()
                uid = os.getuid()
                if (
                    not stat.S_ISDIR(directory_info.st_mode)
                    or directory_info.st_uid != uid
                    or stat.S_IMODE(directory_info.st_mode) != 0o700
                    or not stat.S_ISSOCK(socket_info.st_mode)
                    or socket_info.st_uid != uid
                    or stat.S_IMODE(socket_info.st_mode) != 0o600
                ):
                    raise RuntimeUnavailableError(
                        "Could not securely connect to QLog. Restart QLog and try again."
                    )
            async with asyncio.timeout(RUNTIME_TIMEOUT):
                reader, writer = await self._connect()
                info = await self._exchange(reader, writer, "info", "system.get_info")
                if type(info.get("protocol")) is not int or info["protocol"] != 1:
                    raise RuntimeProtocolError("Unsupported QLog runtime protocol")
                if info.get("application") != "QLog":
                    raise RuntimeProtocolError("Invalid QLog runtime application")
                params = {"sources": sources} if sources is not None else None
                return await self._exchange(reader, writer, "request", method, params)
        except TimeoutError:
            raise RuntimeUnavailableError("QLog did not respond in time. Try again.") from None
        except (FileNotFoundError, ConnectionRefusedError):
            raise RuntimeUnavailableError(
                "QLog is not running. Start QLog to read the current state."
            ) from None
        except (OSError, NotImplementedError):
            raise RuntimeUnavailableError(
                "Could not connect to QLog. Check that QLog is running."
            ) from None
        finally:
            if writer is not None:
                writer.close()
                with suppress(OSError):
                    await writer.wait_closed()

    @staticmethod
    async def _connect() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if sys.platform != "win32":
            return await asyncio.open_unix_connection(
                path=RUNTIME_ENDPOINT, limit=MAX_RESPONSE_BYTES
            )
        loop = asyncio.get_running_loop()
        connect_pipe = getattr(loop, "create_pipe_connection", None)
        if connect_pipe is None:
            raise RuntimeUnavailableError(
                "Could not connect to QLog with this MCP client on Windows."
            )
        reader = asyncio.StreamReader(limit=MAX_RESPONSE_BYTES)
        protocol = asyncio.StreamReaderProtocol(reader)
        transport, _ = await connect_pipe(lambda: protocol, RUNTIME_ENDPOINT)
        return reader, asyncio.StreamWriter(transport, protocol, reader, loop)

    @staticmethod
    async def _exchange(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        request_id: str,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            request["params"] = params
        writer.write(json.dumps(request).encode() + b"\n")
        await writer.drain()
        try:
            line = await reader.readline()
            if not line.endswith(b"\n") or len(line) > MAX_RESPONSE_BYTES:
                raise ValueError
            reply = json.loads(line)
            if (
                not isinstance(reply, dict)
                or reply.get("id") != request_id
                or ("result" in reply) == ("error" in reply)
            ):
                raise ValueError
            if "error" in reply:
                error = reply["error"]
                if params is not None and isinstance(error, dict) and error.get("code") == "invalid_params":
                    raise InvalidQueryError(
                        "QLog rejected the requested source selection. Refresh "
                        "qlog.list_live_sources and use the available names."
                    )
                raise RuntimeProtocolError("QLog runtime rejected the request")
            if not isinstance(reply["result"], dict):
                raise TypeError
            return reply["result"]
        except (ValueError, TypeError):
            raise RuntimeProtocolError("Invalid QLog runtime response") from None
