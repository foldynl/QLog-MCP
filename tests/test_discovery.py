"""Tests for the external QLog database discovery contract."""

import json
from pathlib import Path

from qlog_mcp import discovery as qlog


def test_explicit_database_path_has_priority(monkeypatch) -> None:
    monkeypatch.setenv("QLOG_DB_PATH", "/environment/qlog.sqlite")

    assert qlog.discover_database(Path("~/explicit.sqlite")) == Path.home() / "explicit.sqlite"


def test_structured_discovery_document(monkeypatch, tmp_path) -> None:
    discovery_file = tmp_path / qlog.DISCOVERY_FILENAME
    discovery_file.write_text(
        json.dumps({"protocol": 1, "database": {"path": "/logs/qlog.sqlite", "schema": 42}}),
        encoding="utf-8",
    )
    monkeypatch.delenv("QLOG_DB_PATH", raising=False)
    monkeypatch.setattr(qlog, "default_discovery_paths", lambda: [discovery_file])

    assert qlog.discover_database() == Path("/logs/qlog.sqlite")
