"""Tests for semantic reference-catalog discovery and querying."""

import json
import sqlite3

import pytest
from fastmcp import Client

from qlog_mcp.database import Database
from qlog_mcp.server import create_server


async def test_catalog_schema_discovers_semantic_capabilities(catalog_qlog_database) -> None:
    async with Client(create_server(catalog_qlog_database)) as client:
        result = await client.call_tool("qlog.get_schema", {"domain": "catalog"})

    schema = result.data["catalog"]
    assert set(schema["catalogs"]) == {"pota", "sota", "wwff", "iota", "dxcc"}
    assert schema["catalogs"]["dxcc"]["source_capability"] == "full"
    assert "between" in schema["catalogs"]["sota"]["fields"]["valid_from"]["operators"]
    assert schema["catalogs"]["pota"]["compatible_qso_fields"] == ["pota_ref"]
    assert schema["catalogs"]["dxcc"]["default_order"] == [
        {"field": "code", "direction": "asc"}
    ]
    assert schema["catalogs"]["dxcc"]["comparison_key"] == {
        "field": "code",
        "type": "integer",
        "description": "Catalog field compared with the selected compatible QSO field.",
    }
    assert "do not assume missing fields" in schema["source_capability_semantics"]["reduced"]
    assert "award credit" in schema["compatible_qso_fields_semantics"]
    assert set(schema["match_qso"]["relations"]) == {
        "matched",
        "not_matched",
        "qso_only",
    }
    assert set(schema["match_qso"]["qso_only_fields"]) == {"key", "qso_count"}
    assert set(schema["match_qso"]["summary_fields"]) == {
        "catalog_values",
        "matched_values",
        "not_matched_values",
        "qso_only_values",
    }
    assert schema["match_qso"]["item_semantics"]["qso_only"].endswith(
        "not QSO rows."
    )

    encoded = json.dumps(schema)
    for physical_name in (
        "pota_directory",
        "summit_code",
        "entityID",
        "dxcc_entities_clublog",
    ):
        assert physical_name not in encoded


async def test_catalog_query_filters_sorts_and_pages(catalog_qlog_database) -> None:
    arguments = {
        "catalog": "pota",
        "filters": {
            "logic": "and",
            "conditions": [{"field": "dxcc", "op": "in", "value": [291, 503]}],
            "groups": [
                {
                    "logic": "or",
                    "conditions": [
                        {"field": "name", "op": "contains", "value": "stone"},
                        {"field": "reference", "op": "starts_with", "value": "OK-"},
                    ],
                }
            ],
        },
        "fields": ["reference", "name", "active", "dxcc"],
        "sort": [
            {"field": "active", "direction": "desc"},
            {"field": "name", "direction": "asc"},
        ],
        "limit": 1,
    }
    async with Client(create_server(catalog_qlog_database)) as client:
        first = await client.call_tool("catalog.query", arguments)
        arguments["offset"] = first.data["page"]["next_offset"]
        second = await client.call_tool("catalog.query", arguments)

    assert first.data == {
        "catalog": "pota",
        "items": [
            {"reference": "K-0001", "name": "Yellowstone", "active": True, "dxcc": 291}
        ],
        "fields": ["reference", "name", "active", "dxcc"],
        "page": {
            "limit": 1,
            "offset": 0,
            "returned": 1,
            "has_more": True,
            "next_offset": 1,
        },
    }
    assert second.data["items"][0]["reference"] == "OK-0001"
    assert second.data["page"]["has_more"] is False
    assert second.data["page"]["next_offset"] is None


async def test_catalog_dates_are_normalized(catalog_qlog_database) -> None:
    async with Client(create_server(catalog_qlog_database)) as client:
        sota = await client.call_tool(
            "catalog.query",
            {
                "catalog": "sota",
                "fields": ["reference", "valid_from", "valid_to"],
                "sort": [{"field": "reference"}],
            },
        )
        wwff = await client.call_tool(
            "catalog.query",
            {
                "catalog": "wwff",
                "fields": ["reference", "valid_from", "valid_to"],
                "sort": [{"field": "reference"}],
            },
        )
        dxcc = await client.call_tool(
            "catalog.query",
            {
                "catalog": "dxcc",
                "filters": {"conditions": [{"field": "code", "op": "eq", "value": 2}]},
                "fields": ["code", "valid_from", "valid_to"],
            },
        )

    assert sota.data["items"] == [
        {"reference": "OK/PA-001", "valid_from": "2007-03-01", "valid_to": "2099-12-31"},
        {"reference": "W1/AM-001", "valid_from": None, "valid_to": None},
    ]
    assert wwff.data["items"] == [
        {"reference": "KFF-0001", "valid_from": "2013-01-01", "valid_to": None},
        {"reference": "OKFF-0001", "valid_from": None, "valid_to": None},
    ]
    assert dxcc.data["items"] == [
        {"code": 2, "valid_from": None, "valid_to": "1991-03-30"}
    ]


async def test_catalog_queries_all_initial_mappings_and_prefers_full_dxcc_source(
    catalog_qlog_database,
) -> None:
    async with Client(create_server(catalog_qlog_database)) as client:
        iota = await client.call_tool(
            "catalog.query",
            {
                "catalog": "iota",
                "filters": {
                    "conditions": [{"field": "reference", "op": "eq", "value": "eu-001"}]
                },
            },
        )
        dxcc = await client.call_tool(
            "catalog.query",
            {
                "catalog": "dxcc",
                "filters": {"conditions": [{"field": "code", "op": "eq", "value": 291}]},
                "fields": ["code", "name", "deleted", "valid_from"],
            },
        )

    assert iota.data["items"] == [{"reference": "EU-001", "name": "Dodecanese"}]
    assert dxcc.data["items"] == [
        {
            "code": 291,
            "name": "United States",
            "deleted": False,
            "valid_from": "1945-11-15",
        }
    ]


async def test_reduced_catalogs_hide_missing_fields_and_use_dxcc_fallback(
    reduced_catalog_database,
) -> None:
    async with Client(create_server(reduced_catalog_database)) as client:
        schema = await client.call_tool("qlog.get_schema", {"domain": "catalog"})
        unavailable_field = await client.call_tool(
            "catalog.query",
            {"catalog": "pota", "fields": ["reference", "active"]},
            raise_on_error=False,
        )
        unavailable_catalog = await client.call_tool(
            "catalog.query", {"catalog": "sota"}, raise_on_error=False
        )
        dxcc = await client.call_tool(
            "catalog.query", {"catalog": "dxcc", "fields": ["code", "name"]}
        )

    catalogs = schema.data["catalog"]["catalogs"]
    assert set(catalogs) == {"pota", "dxcc"}
    assert set(catalogs["pota"]["fields"]) == {"reference", "name"}
    assert catalogs["pota"]["source_capability"] == "reduced"
    assert catalogs["dxcc"]["source_capability"] == "reduced"
    assert "deleted" not in catalogs["dxcc"]["fields"]
    assert dxcc.data["items"] == [{"code": 291, "name": "United States"}]
    assert unavailable_field.is_error is True
    assert "Catalog field 'active' is unavailable" in unavailable_field.content[0].text
    assert unavailable_catalog.is_error is True
    assert "Catalog 'sota' is unavailable" in unavailable_catalog.content[0].text


async def test_catalog_query_usage_log_keeps_filter_values_private(
    catalog_qlog_database, tmp_path
) -> None:
    usage_log = tmp_path / "catalog-usage.jsonl"
    secret_value = "Yellowstone"

    async with Client(create_server(catalog_qlog_database, usage_log)) as client:
        await client.call_tool(
            "catalog.query",
            {
                "catalog": "pota",
                "filters": {
                    "conditions": [{"field": "name", "op": "eq", "value": secret_value}]
                },
                "fields": ["reference", "name"],
                "limit": 10,
            },
        )

    raw_log = usage_log.read_text()
    event = json.loads(raw_log)
    assert secret_value not in raw_log
    assert event["arguments"] == {
        "catalog": "pota",
        "filters": {
            "group_count": 1,
            "conditions": [{"field": "name", "op": "eq"}],
        },
        "fields": ["reference", "name"],
        "sort": None,
        "limit": 10,
        "offset": 0,
    }
    assert "?" in event["sql"][0]["statement"]
    assert event["sql"][0]["parameter_types"] == ["str", "int", "int"]
    assert event["sql"][0]["returned_rows"] == 1


async def test_catalog_registration_is_lazy_and_database_remains_read_only(tmp_path) -> None:
    missing = tmp_path / "not-created.sqlite"
    tools = await create_server(missing).list_tools()
    assert any(tool.name == "catalog.query" for tool in tools)
    assert not missing.exists()

    database = tmp_path / "existing.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE sample (value INTEGER)")
    connection.close()
    async with Database(database).connect() as read_only:
        pragma = await read_only.execute_fetchall("PRAGMA query_only")
        assert pragma[0][0] == 1
        with pytest.raises(sqlite3.OperationalError):
            await read_only.execute("INSERT INTO sample VALUES (1)")
