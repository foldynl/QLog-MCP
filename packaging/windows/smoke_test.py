"""Check the packaged CLI and MCP startup without an existing database."""

import asyncio
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from fastmcp import Client
from fastmcp.client.transports import StdioTransport


async def check_mcp(artifact: Path) -> None:
    with TemporaryDirectory() as directory:
        database = Path(directory) / "missing.db"
        transport = StdioTransport(
            command=str(artifact), args=["--database", str(database)], cwd=directory, keep_alive=False
        )
        async with Client(transport, timeout=30) as client:
            tools = await client.list_tools()
            assert {"qlog.get_context", "qso.query"} <= {tool.name for tool in tools}
        assert not database.exists(), "MCP startup must not create a database"


if __name__ == "__main__":
    artifact = Path(sys.argv[1]).resolve()
    result = subprocess.run([str(artifact), "--version"], check=True, capture_output=True, text=True)
    assert result.stdout.strip() == f"qlog-mcp {sys.argv[2]}", result.stdout
    asyncio.run(check_mcp(artifact))
