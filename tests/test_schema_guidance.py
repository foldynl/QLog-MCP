"""Public guidance for confirmation, known timestamps and catalog matching."""

import sqlite3
from pathlib import Path

import pytest
from fastmcp import Client

from qlog_mcp.catalog import CatalogQuery, MatchRelation
from qlog_mcp.database import Database
from qlog_mcp.errors import InvalidQueryError
from qlog_mcp.qso import LogScope, QsoQuery
from qlog_mcp.server import create_server


@pytest.mark.parametrize("format", ["full", "compact"])
async def test_received_confirmation_guidance_includes_legacy_verified_records(
    qlog_database: Path, format: str
) -> None:
    with sqlite3.connect(qlog_database) as connection:
        connection.executemany(
            "UPDATE contacts SET lotw_qsl_rcvd = ? WHERE id = ?",
            [("Y", 1), ("V", 2), ("N", 3), ("I", 4)],
        )
    async with Client(create_server(qlog_database)) as client:
        schema = (await client.call_tool("qlog.get_schema", {"format": format})).data["qso"]
        for name in ["qsl_received", "lotw_received"]:
            field = schema["fields"][name]
            enum = schema["enums"][field["enum_ref"]] if format == "compact" else field["enum"]
            verified = enum["values"]["V"]
            assert "include with Y" in verified
            assert "stored-confirmation" in verified
            assert "DXCC" in verified
            assert "import-only" in verified
        for operator, value, expected in [("in", ["Y", "V"], 2), ("eq", "Y", 1)]:
            result = await client.call_tool(
                "qso.aggregate",
                {
                    "scope": {"station_scope": "all"},
                    "group_by": [],
                    "filters": {
                        "conditions": [{"field": "lotw_received", "op": operator, "value": value}]
                    },
                    "metrics": [{"function": "count", "as": "confirmations"}],
                },
            )
            assert result.data["rows"] == [{"confirmations": expected}]


@pytest.mark.parametrize("field,column", [("datetime", "start_time"), ("datetime_off", "end_time")])
@pytest.mark.parametrize("direction,expected_id", [("asc", 1), ("desc", 2)])
async def test_known_time_guidance_excludes_missing_and_invalid_timestamps(
    qlog_database: Path, field: str, column: str, direction: str, expected_id: int
) -> None:
    with sqlite3.connect(qlog_database) as connection:
        connection.execute(f'UPDATE contacts SET "{column}" = NULL WHERE id = 4')
        connection.execute(f'UPDATE contacts SET "{column}" = ? WHERE id = 3', ["not-a-date"])
    arguments = {
        "scope": {"station_scope": "all"},
        "fields": ["id", field],
        "sort": [{"field": field, "direction": direction}],
        "limit": 1,
    }
    async with Client(create_server(qlog_database)) as client:
        schema = (await client.call_tool("qlog.get_schema", {"format": "compact"})).data["qso"]
        assert "is_not_null" in schema["fields"][field]["description"]
        assert "null" in schema["fields"][field]["description"]
        if direction == "asc":
            unfiltered = await client.call_tool("qso.query", arguments)
            assert unfiltered.data["items"][0][field] is None
        arguments["filters"] = {"conditions": [{"field": field, "op": "is_not_null"}]}
        known = await client.call_tool("qso.query", arguments)
        assert known.data["items"][0]["id"] == expected_id
        with sqlite3.connect(qlog_database) as connection:
            connection.execute(f'UPDATE contacts SET "{column}" = NULL')
        unknown = await client.call_tool("qso.query", arguments)
        assert unknown.data["items"] == []
        context = await client.call_tool("qlog.get_context")
        assert context.data["qso_count"] == 4


async def test_tool_schemas_explain_null_order_and_advertise_only_matchable_catalogs() -> None:
    async with Client(create_server()) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    match = tools["catalog.match_qso"].input_schema["properties"]["catalog"]
    query = tools["catalog.query"].input_schema["properties"]["catalog"]
    assert set(match["enum"]) == {"pota", "sota", "wwff", "iota", "dxcc", "satellite"}
    assert set(query["enum"]) == set(match["enum"]) | {"membership", "membership_clubs"}
    assert "membership.match_qso" in match["description"]
    assert "membership_clubs" in match["description"]
    direction = tools["qso.query"].input_schema["$defs"]["SortSpec"]["properties"]["direction"]
    assert "NULL" in direction["description"]
    assert "first" in direction["description"]
    assert "last" in direction["description"]


@pytest.mark.parametrize("catalog", ["membership", "membership_clubs"])
async def test_unmatchable_catalog_is_rejected_before_database_access(
    tmp_path: Path, catalog: str
) -> None:
    database = tmp_path / "missing.sqlite"
    async with Client(create_server(database)) as client:
        result = await client.call_tool(
            "catalog.match_qso",
            {"catalog": catalog, "qso_field": "callsign", "scope": {"station_scope": "all"}},
            raise_on_error=False,
        )
    assert result.is_error
    assert "Input should be" in result.content[0].text
    assert "Database" not in result.content[0].text
    assert not database.exists()


async def test_backend_membership_rejection_points_to_roster_matching(
    catalog_qlog_database: Path,
) -> None:
    database = Database(catalog_qlog_database)
    catalogs = CatalogQuery(database, QsoQuery(database))
    with pytest.raises(InvalidQueryError, match="membership.match_qso"):
        await catalogs.match_qso(
            catalog="membership",
            qso_field="callsign",
            scope=LogScope(station_scope="all"),
            qso_filters=None,
            catalog_filters=None,
            partition_by=None,
            relation=MatchRelation.MATCHED,
            fields=None,
            sort=None,
            limit=100,
            offset=0,
        )
