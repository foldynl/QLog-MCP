"""Geographic catalog search through the public MCP contract."""

import json
import math
import sqlite3

import pytest
from fastmcp import Client

from qlog_mcp.server import create_server


@pytest.fixture
def nearby_catalog(catalog_qlog_database):
    with sqlite3.connect(catalog_qlog_database) as connection:
        connection.execute("DELETE FROM pota_directory")
        connection.executemany(
            "INSERT INTO pota_directory (reference, name, active, latitude, longitude) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                ("CZ-0001", "At centre", 1, 50.020833333333336, 14.041666666666666),
                ("CZ-0002", "Near north", 1, 50.1, 14.041666666666666),
                ("CZ-0003", "Further north", 1, 50.2, 14.041666666666666),
                ("CZ-0004", "Outside radius", 1, 51.0, 14.0),
                ("CZ-0005", "Unknown location", 1, None, 14.0),
                ("CZ-0006", "Invalid location", 1, "not-a-number", 14.0),
                ("CZ-0007", "Out of range", 1, 100.0, 14.0),
            ],
        )
        connection.execute("ALTER TABLE contacts ADD COLUMN my_pota_ref TEXT")
        connection.execute(
            "UPDATE contacts SET my_pota_ref = ' cz-0001@CZ-XX , CZ-0004' WHERE id = 1"
        )
        connection.execute("UPDATE contacts SET band = '20m'")
    return catalog_qlog_database


async def test_near_query_filters_before_paging_and_returns_effective_origin(nearby_catalog):
    arguments = {"catalog": "pota", "near": {"grid": "jo70aa", "radius_km": 30}, "limit": 2}
    async with Client(create_server(nearby_catalog)) as client:
        first = (await client.call_tool("catalog.query", arguments)).data
        second = (
            await client.call_tool(
                "catalog.query", {**arguments, "offset": first["page"]["next_offset"]}
            )
        ).data
    assert [row["reference"] for row in first["items"]] == ["CZ-0001", "CZ-0002"]
    assert [row["reference"] for row in second["items"]] == ["CZ-0003"]
    assert first["items"][0]["distance_km"] == pytest.approx(0, abs=1e-6)
    assert 8 < first["items"][1]["distance_km"] < 10
    assert "distance_km" in first["fields"]
    assert first["effective_near"] == {
        "grid": "JO70AA",
        "latitude": pytest.approx(50.020833333333336),
        "longitude": pytest.approx(14.041666666666666),
        "radius_km": 30,
    }
    assert second["page"]["has_more"] is False


@pytest.mark.parametrize("partition_by", [None, ["band"]])
async def test_near_matching_reuses_qso_sets_and_full_summaries(nearby_catalog, partition_by):
    arguments = {
        "catalog": "pota",
        "near": {"grid": "JO70AA", "radius_km": 30},
        "qso_field": "my_pota_ref",
        "scope": {"station_scope": "all"},
        "relation": "not_matched",
        "fields": ["reference", "name", "distance_km", "qso_count"],
        "limit": 1,
    }
    if partition_by:
        arguments["partition_by"] = partition_by
    async with Client(create_server(nearby_catalog)) as client:
        first = (await client.call_tool("catalog.match_qso", arguments)).data
        second = (
            await client.call_tool(
                "catalog.match_qso", {**arguments, "offset": first["page"]["next_offset"]}
            )
        ).data
    summary = first["summaries"][0]["summary"] if partition_by else first["summary"]
    assert summary["catalog_values"] == 3
    assert summary["matched_values"] == 1
    assert summary["not_matched_values"] == 2
    assert first["items"][0]["reference"] == "CZ-0002"
    assert second["items"][0]["reference"] == "CZ-0003"
    assert first["items"][0]["qso_count"] == 0
    assert first["effective_near"]["grid"] == "JO70AA"


@pytest.mark.parametrize("tool", ["catalog.query", "catalog.match_qso"])
@pytest.mark.parametrize(
    "near",
    [
        {},
        {"grid": "JO70AA", "radius_km": True},
        {"grid": "JO70AA", "radius_km": 0},
        {"grid": "JO70AA", "radius_km": "30"},
        {"grid": "invalid", "radius_km": 30},
        {"grid": "JS70AA", "radius_km": 30},
        {"latitude": 50, "radius_km": 30},
        {"latitude": True, "longitude": 14, "radius_km": 30},
        {"latitude": 91, "longitude": 14, "radius_km": 30},
        {"grid": "JO70AA", "latitude": 50, "longitude": 14, "radius_km": 30},
        {"grid": "JO70AA", "radius_km": 30, "typo": 1},
    ],
)
async def test_near_rejects_invalid_requests_before_database_access(tmp_path, tool, near):
    arguments = {"catalog": "pota", "near": near}
    if tool == "catalog.match_qso":
        arguments.update(qso_field="my_pota_ref", scope={"station_scope": "all"})
    async with Client(create_server(tmp_path / "missing.sqlite")) as client:
        result = await client.call_tool(tool, arguments, raise_on_error=False)
    assert result.is_error
    assert "does not exist" not in str(result.content)


async def test_near_point_distance_at_dateline_pole_and_zero_coordinates(nearby_catalog):
    with sqlite3.connect(nearby_catalog) as connection:
        connection.execute("DELETE FROM pota_directory")
        connection.executemany(
            "INSERT INTO pota_directory (reference, latitude, longitude) VALUES (?, ?, ?)",
            [("A", 0, -179.9), ("B", 0, 179.9), ("C", 89.9, -170), ("D", 0, 0)],
        )
    async with Client(create_server(nearby_catalog)) as client:

        async def search(latitude, longitude, radius):
            return (
                await client.call_tool(
                    "catalog.query",
                    {
                        "catalog": "pota",
                        "near": {
                            "latitude": latitude,
                            "longitude": longitude,
                            "radius_km": radius,
                        },
                        "fields": ["reference", "distance_km"],
                    },
                )
            ).data

        dateline = await search(0, 180, 12)
        pole = await search(90, 10, 12)
        zero = await search(0, 0, 1)
        world = await search(0, 0, 21000)
    assert {row["reference"] for row in dateline["items"]} == {"A", "B"}
    assert [row["reference"] for row in pole["items"]] == ["C"]
    assert zero["items"] == [{"reference": "D", "distance_km": 0}]
    assert len(world["items"]) == 4
    assert all(math.isfinite(row["distance_km"]) for row in world["items"])


@pytest.mark.parametrize(
    "latitude,longitude,park_latitude,park_longitude,radius",
    [
        (0, 179.9, 0, -179.83020389088261, 30),
        (-89.99999939, 0, 89.99999939, 0, 20015.114252165065),
    ],
)
async def test_near_bounding_box_does_not_drop_a_point_on_the_radius(
    nearby_catalog,
    latitude,
    longitude,
    park_latitude,
    park_longitude,
    radius,
):
    with sqlite3.connect(nearby_catalog) as connection:
        connection.execute("DELETE FROM pota_directory")
        connection.execute(
            "INSERT INTO pota_directory (reference, latitude, longitude) VALUES ('EDGE', ?, ?)",
            (park_latitude, park_longitude),
        )
    async with Client(create_server(nearby_catalog)) as client:
        result = (
            await client.call_tool(
                "catalog.query",
                {
                    "catalog": "pota",
                    "near": {
                        "latitude": latitude,
                        "longitude": longitude,
                        "radius_km": radius,
                    },
                    "fields": ["reference", "distance_km"],
                },
            )
        ).data
    assert result["items"] == [{"reference": "EDGE", "distance_km": round(radius, 2)}]


async def test_near_schema_is_shared_and_values_are_not_logged(nearby_catalog, tmp_path):
    log = tmp_path / "usage.jsonl"
    async with Client(create_server(nearby_catalog, log)) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        schema = (await client.call_tool("qlog.get_schema", {"domain": "catalog"})).data["catalog"]
        await client.call_tool(
            "catalog.query",
            {
                "catalog": "pota",
                "near": {"grid": "JO70AA", "radius_km": 31.23456789},
            },
        )
    for name in ("catalog.query", "catalog.match_qso"):
        assert "near" in tools[name].input_schema["properties"]
        assert "near" not in tools[name].input_schema.get("required", [])
    assert schema["catalogs"]["pota"]["supports_near"] is True
    assert schema["near"]["distance_field"] == "distance_km"
    assert "my_pota_ref" in json.dumps(schema["near"])
    encoded = log.read_text()
    assert "JO70AA" not in encoded
    assert "31.23456789" not in encoded
    assert "50.020833" not in encoded
    assert "14.041666" not in encoded


@pytest.mark.parametrize("catalog", ["sota", "wwff"])
async def test_near_other_point_catalogs_and_explicit_projection(catalog_qlog_database, catalog):
    async with Client(create_server(catalog_qlog_database)) as client:
        result = (
            await client.call_tool(
                "catalog.query",
                {
                    "catalog": catalog,
                    "near": {"latitude": 50, "longitude": 15, "radius_km": 500},
                    "fields": ["reference"],
                },
            )
        ).data
    assert len(result["items"]) == 1
    assert set(result["items"][0]) == {"reference"}
    assert result["effective_near"] == {"latitude": 50, "longitude": 15, "radius_km": 500}


async def test_near_matched_statistics_catalog_filters_and_scope(nearby_catalog):
    arguments = {
        "catalog": "pota",
        "near": {"grid": "JO70AA", "radius_km": 30},
        "scope": {"station_scope": "all"},
        "qso_field": "my_pota_ref",
        "relation": "matched",
        "fields": ["reference", "qso_count", "first_qso", "last_qso"],
        "catalog_filters": {"conditions": [{"field": "active", "op": "eq", "value": True}]},
    }
    async with Client(create_server(nearby_catalog)) as client:
        matched = (await client.call_tool("catalog.match_qso", arguments)).data
        scoped = (
            await client.call_tool(
                "catalog.match_qso",
                {
                    **arguments,
                    "scope": {
                        "station_scope": "profile",
                        "station_profile_names": ["Home"],
                    },
                },
            )
        ).data
        filtered = (
            await client.call_tool(
                "catalog.match_qso",
                {
                    **arguments,
                    "qso_filters": {
                        "conditions": [{"field": "mode", "op": "eq", "value": "SSB"}],
                    },
                },
            )
        ).data
    assert matched["items"] == [
        {
            "reference": "CZ-0001",
            "qso_count": 1,
            "first_qso": "2025-12-31T23:59:00Z",
            "last_qso": "2025-12-31T23:59:00Z",
        }
    ]
    assert scoped["items"] == filtered["items"] == []
    assert scoped["summary"]["not_matched_values"] == 3


async def test_near_distance_sort_filter_ties_and_empty_result(nearby_catalog):
    with sqlite3.connect(nearby_catalog) as connection:
        connection.execute(
            "INSERT INTO pota_directory (reference, latitude, longitude) "
            "SELECT 'CZ-0000', latitude, longitude FROM pota_directory WHERE reference = 'CZ-0002'"
        )
    async with Client(create_server(nearby_catalog)) as client:
        tied = (
            await client.call_tool(
                "catalog.query",
                {
                    "catalog": "pota",
                    "near": {"grid": "JO70AA", "radius_km": 30},
                    "fields": ["reference"],
                    "filters": {
                        "conditions": [{"field": "distance_km", "op": "between", "value": [1, 15]}]
                    },
                },
            )
        ).data
        empty = (
            await client.call_tool(
                "catalog.query",
                {
                    "catalog": "pota",
                    "near": {"latitude": -70, "longitude": 10, "radius_km": 1},
                },
            )
        ).data
    assert tied["items"] == [{"reference": "CZ-0000"}, {"reference": "CZ-0002"}]
    assert empty["items"] == []
    assert empty["page"]["has_more"] is False
    assert "effective_near" in empty


async def test_near_requires_available_coordinates_and_rejects_qso_only(nearby_catalog):
    async with Client(create_server(nearby_catalog)) as client:
        for arguments, message in [
            ({"catalog": "iota", "near": {"grid": "JO70AA", "radius_km": 30}}, "POTA"),
            ({"catalog": "pota", "fields": ["distance_km"]}, "requires near"),
        ]:
            result = await client.call_tool("catalog.query", arguments, raise_on_error=False)
            assert result.is_error and message in str(result.content)
        result = await client.call_tool(
            "catalog.match_qso",
            {
                "catalog": "pota",
                "near": {"grid": "JO70AA", "radius_km": 30},
                "qso_field": "my_pota_ref",
                "scope": {"station_scope": "all"},
                "relation": "qso_only",
            },
            raise_on_error=False,
        )
        assert result.is_error and "qso_only" in str(result.content)
    with sqlite3.connect(nearby_catalog) as connection:
        connection.execute("ALTER TABLE pota_directory DROP COLUMN latitude")
    async with Client(create_server(nearby_catalog)) as client:
        schema = (await client.call_tool("qlog.get_schema", {"domain": "catalog"})).data["catalog"]
        result = await client.call_tool(
            "catalog.query",
            {
                "catalog": "pota",
                "near": {"grid": "JO70AA", "radius_km": 30},
            },
            raise_on_error=False,
        )
        ordinary = (await client.call_tool("catalog.query", {"catalog": "pota"})).data
    assert not schema["catalogs"]["pota"].get("supports_near", False)
    assert result.is_error and "requires latitude and longitude" in str(result.content)
    assert len(ordinary["items"]) == 7
    assert "effective_near" not in ordinary


@pytest.mark.parametrize(
    "grid,latitude,longitude",
    [
        ("JO70", 50.5, 15),
        ("JO70AA", 50 + 1 / 48, 14 + 1 / 24),
        ("JO70AA00", 50 + 1 / 480, 14 + 1 / 240),
        ("JO70AA00AA", 50 + 1 / 11520, 14 + 1 / 5760),
        ("JO70AA00AA00", 50 + 1 / 115200, 14 + 1 / 57600),
    ],
)
def test_near_locator_precision(grid, latitude, longitude):
    from qlog_mcp.geography import NearSearch

    assert NearSearch(grid=grid, radius_km=1).centre() == pytest.approx(
        (latitude, longitude),
        abs=1e-10,
        rel=0,
    )


def test_near_requires_finite_numbers_without_coercion():
    from pydantic import ValidationError

    from qlog_mcp.geography import NearSearch

    for key in ("radius_km", "latitude", "longitude"):
        for invalid in (True, "1", float("inf"), float("nan")):
            arguments = {"latitude": 0, "longitude": 0, "radius_km": 1, key: invalid}
            with pytest.raises(ValidationError):
                NearSearch(**arguments)
