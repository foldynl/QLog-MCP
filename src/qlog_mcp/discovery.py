"""Resolve the QLog database path without duplicating QLog's Qt path logic."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

DISCOVERY_FILENAME = "qlog-service.json"


def default_discovery_paths() -> list[Path]:
    """Return documented platform locations for the QLog discovery document."""
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return [root / "QLog" / DISCOVERY_FILENAME]

    if sys.platform == "darwin":
        return [Path.home() / "Library" / "Application Support" / "QLog" / DISCOVERY_FILENAME]

    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return [root / "qlog" / DISCOVERY_FILENAME]


def _database_from_document(document: dict[str, Any]) -> Path | None:
    database = document.get("database")
    if isinstance(database, str):
        return Path(database).expanduser()
    if isinstance(database, dict) and isinstance(database.get("path"), str):
        return Path(database["path"]).expanduser()
    return None


def read_discovery_file(path: Path) -> Path | None:
    """Read a discovery file and return its declared database path."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return _database_from_document(document) if isinstance(document, dict) else None


def discover_database(explicit_path: Path | None = None) -> Path | None:
    """Resolve a database from CLI, environment, then QLog's discovery file."""
    if explicit_path is not None:
        return explicit_path.expanduser()

    if environment_path := os.environ.get("QLOG_DB_PATH"):
        return Path(environment_path).expanduser()

    for discovery_path in default_discovery_paths():
        if database_path := read_discovery_file(discovery_path):
            return database_path

    return None
