"""Command-line entry point for the QLog MCP server."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from importlib.metadata import version
from pathlib import Path

from .server import create_server


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="qlog-mcp", description="Read-only MCP server for QLog")
    parser.add_argument("--version", action="version", version=f"%(prog)s {version('qlog-mcp')}")
    parser.add_argument("--database", type=Path, help="path to the QLog SQLite database")
    parser.add_argument(
        "--usage-log",
        type=Path,
        help="write privacy-conscious MCP usage metrics as JSON Lines",
    )
    args = parser.parse_args(argv)

    create_server(args.database, args.usage_log).run()


if __name__ == "__main__":
    main()
