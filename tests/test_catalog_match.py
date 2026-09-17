"""Tests for neutral catalog/QSO set comparisons."""

import json
import sqlite3

from fastmcp import Client

from qlog_mcp.server import create_server


async def test_matches_scalar_dxcc_keys_and_applies_independent_filters(
    catalog_match_database,
) -> None:
    arguments = {
        "catalog": "dxcc",
        "qso_field": "dxcc",
        "scope": {"station_scope": "all"},
        "qso_filters": {
            "logic": "or",
            "conditions": [
                {"field": "lotw_received", "op": "eq", "value": "Y"},
                {"field": "qsl_received", "op": "eq", "value": "Y"},
            ],
        },
        "catalog_filters": {
            "conditions": [{"field": "deleted", "op": "eq", "value": False}]
        },
        "fields": ["code", "name"],
    }
    async with Client(create_server(catalog_match_database)) as client:
        matched = await client.call_tool(
            "catalog.match_qso", {**arguments, "relation": "matched"}
        )
        not_matched = await client.call_tool(
            "catalog.match_qso", {**arguments, "relation": "not_matched"}
        )
        qso_only = await client.call_tool(
            "catalog.match_qso",
            {
                **arguments,
                "relation": "qso_only",
                "fields": ["key", "qso_count"],
            },
        )

    assert matched.data["summary"] == not_matched.data["summary"] == qso_only.data[
        "summary"
    ] == {
        "catalog_values": 2,
        "matched_values": 1,
        "not_matched_values": 1,
        "qso_only_values": 0,
    }
    assert matched.data["items"] == [{"code": 339, "name": "Japan"}]
    assert not_matched.data["items"] == [{"code": 291, "name": "United States"}]
    assert qso_only.data["items"] == []


async def test_partitions_catalog_comparison_before_qso_filters(
    catalog_match_database,
) -> None:
    arguments = {
        "catalog": "dxcc",
        "qso_field": "dxcc",
        "scope": {"station_scope": "all"},
        "qso_filters": {
            "logic": "or",
            "conditions": [
                {"field": "lotw_received", "op": "eq", "value": "Y"},
                {"field": "qsl_received", "op": "eq", "value": "Y"},
            ],
        },
        "catalog_filters": {
            "conditions": [{"field": "deleted", "op": "eq", "value": False}]
        },
        "partition_by": ["band", "mode"],
        "relation": "not_matched",
        "fields": ["code", "name"],
        "limit": 3,
    }
    async with Client(create_server(catalog_match_database)) as client:
        first = await client.call_tool("catalog.match_qso", arguments)
        second = await client.call_tool(
            "catalog.match_qso",
            {**arguments, "offset": first.data["page"]["next_offset"]},
        )

    expected_summaries = [
        {
            "partition": {"band": "15m", "mode": "FT8"},
            "summary": {
                "catalog_values": 2,
                "matched_values": 1,
                "not_matched_values": 1,
                "qso_only_values": 0,
            },
        },
        {
            "partition": {"band": "20m", "mode": "CW"},
            "summary": {
                "catalog_values": 2,
                "matched_values": 1,
                "not_matched_values": 1,
                "qso_only_values": 0,
            },
        },
        {
            "partition": {"band": "20m", "mode": "SSB"},
            "summary": {
                "catalog_values": 2,
                "matched_values": 0,
                "not_matched_values": 2,
                "qso_only_values": 0,
            },
        },
    ]
    assert first.data["partition_by"] == ["band", "mode"]
    assert first.data["summaries"] == second.data["summaries"] == expected_summaries
    assert first.data["items"] == [
        {
            "partition": {"band": "15m", "mode": "FT8"},
            "code": 291,
            "name": "United States",
        },
        {
            "partition": {"band": "20m", "mode": "CW"},
            "code": 291,
            "name": "United States",
        },
        {
            "partition": {"band": "20m", "mode": "SSB"},
            "code": 291,
            "name": "United States",
        },
    ]
    assert first.data["page"]["has_more"] is True
    assert second.data["items"] == [
        {
            "partition": {"band": "20m", "mode": "SSB"},
            "code": 339,
            "name": "Japan",
        }
    ]


async def test_partitioned_list_keys_keep_item_semantics(
    catalog_match_database,
) -> None:
    async with Client(create_server(catalog_match_database)) as client:
        result = await client.call_tool(
            "catalog.match_qso",
            {
                "catalog": "pota",
                "qso_field": "pota_ref",
                "scope": {"station_scope": "all"},
                "partition_by": ["band"],
                "relation": "qso_only",
            },
        )

    assert result.data["summaries"] == [
        {
            "partition": {"band": "15m"},
            "summary": {
                "catalog_values": 3,
                "matched_values": 0,
                "not_matched_values": 3,
                "qso_only_values": 1,
            },
        },
        {
            "partition": {"band": "20m"},
            "summary": {
                "catalog_values": 3,
                "matched_values": 2,
                "not_matched_values": 1,
                "qso_only_values": 0,
            },
        },
    ]
    assert result.data["items"] == [
        {"partition": {"band": "15m"}, "key": "JA-0001", "qso_count": 1}
    ]


async def test_partitioned_match_keeps_qso_statistics_inside_each_partition(
    catalog_match_database,
) -> None:
    async with Client(create_server(catalog_match_database)) as client:
        result = await client.call_tool(
            "catalog.match_qso",
            {
                "catalog": "dxcc",
                "qso_field": "dxcc",
                "scope": {"station_scope": "all"},
                "partition_by": ["band", "mode"],
                "relation": "matched",
                "fields": ["code", "qso_count", "first_qso", "last_qso"],
            },
        )

    assert result.data["items"] == [
        {
            "partition": {"band": "15m", "mode": "FT8"},
            "code": 339,
            "qso_count": 1,
            "first_qso": "2026-01-15T12:30:00Z",
            "last_qso": "2026-01-15T12:30:00Z",
        },
        {
            "partition": {"band": "20m", "mode": "CW"},
            "code": 339,
            "qso_count": 2,
            "first_qso": "2025-12-31T23:59:00Z",
            "last_qso": "2026-02-20T08:00:00Z",
        },
        {
            "partition": {"band": "20m", "mode": "SSB"},
            "code": 291,
            "qso_count": 1,
            "first_qso": "2026-02-21T09:00:00Z",
            "last_qso": "2026-02-21T09:00:00Z",
        },
    ]


async def test_partitioned_match_returns_empty_result_for_empty_scope(
    catalog_match_database,
) -> None:
    async with Client(create_server(catalog_match_database)) as client:
        result = await client.call_tool(
            "catalog.match_qso",
            {
                "catalog": "dxcc",
                "qso_field": "dxcc",
                "scope": {"station_scope": "all", "date_from": "2030-01-01"},
                "partition_by": ["band"],
                "relation": "not_matched",
            },
        )

    assert result.data["summaries"] == []
    assert result.data["items"] == []
    assert result.data["page"]["has_more"] is False


async def test_partitioned_match_rejects_unsupported_or_excessive_partitions(
    catalog_match_database,
) -> None:
    connection = sqlite3.connect(catalog_match_database)
    connection.executemany(
        """
        INSERT INTO contacts (
            id, start_time, callsign, band, mode, dxcc, station_callsign
        ) VALUES (?, '2026-03-01T00:00:00Z', ?, '20m', 'CW', 291, 'OK1MLG')
        """,
        [(1000 + index, f"TEST{index}") for index in range(101)],
    )
    connection.commit()
    connection.close()

    base = {
        "catalog": "dxcc",
        "qso_field": "dxcc",
        "scope": {"station_scope": "all"},
        "relation": "matched",
    }
    async with Client(create_server(catalog_match_database)) as client:
        unsupported = await client.call_tool(
            "catalog.match_qso",
            {**base, "partition_by": ["pota_ref"]},
            raise_on_error=False,
        )
        excessive = await client.call_tool(
            "catalog.match_qso",
            {**base, "partition_by": ["callsign"]},
            raise_on_error=False,
        )

    assert unsupported.is_error is True
    assert "cannot be used" in unsupported.content[0].text
    assert excessive.is_error is True
    assert "more than 100 partitions" in excessive.content[0].text


async def test_all_relations_and_complete_paginated_summary(
    catalog_match_database,
) -> None:
    base = {
        "catalog": "pota",
        "qso_field": "pota_ref",
        "scope": {"station_scope": "all"},
        "qso_filters": {
            "conditions": [{"field": "callsign", "op": "eq", "value": "NO0CALL"}]
        },
        "fields": ["reference"],
        "sort": [{"field": "name", "direction": "asc"}],
        "limit": 1,
    }
    async with Client(create_server(catalog_match_database)) as client:
        matched = await client.call_tool(
            "catalog.match_qso", {**base, "relation": "matched"}
        )
        first = await client.call_tool(
            "catalog.match_qso", {**base, "relation": "not_matched"}
        )
        second = await client.call_tool(
            "catalog.match_qso",
            {
                **base,
                "relation": "not_matched",
                "offset": first.data["page"]["next_offset"],
            },
        )
        qso_only = await client.call_tool(
            "catalog.match_qso",
            {
                **base,
                "relation": "qso_only",
                "fields": ["key", "qso_count"],
                "sort": [{"field": "key"}],
            },
        )

    expected_summary = {
        "catalog_values": 3,
        "matched_values": 0,
        "not_matched_values": 3,
        "qso_only_values": 0,
    }
    assert matched.data["items"] == []
    assert first.data["summary"] == second.data["summary"] == expected_summary
    assert first.data["items"] == [{"reference": "OK-0001"}]
    assert first.data["page"]["has_more"] is True
    assert second.data["items"] == [{"reference": "K-0001"}]
    assert qso_only.data["items"] == []


async def test_list_keys_use_item_semantics_and_qso_only_counts(
    catalog_match_database,
) -> None:
    base = {
        "catalog": "pota",
        "qso_field": "pota_ref",
        "scope": {"station_scope": "all"},
    }
    async with Client(create_server(catalog_match_database)) as client:
        matched = await client.call_tool(
            "catalog.match_qso",
            {
                **base,
                "relation": "matched",
                "fields": ["reference", "qso_count"],
                "sort": [{"field": "qso_count", "direction": "desc"}],
            },
        )
        logging_side = await client.call_tool(
            "catalog.match_qso",
            {
                **base,
                "qso_field": "my_pota_ref",
                "relation": "matched",
                "fields": ["reference"],
            },
        )
        qso_only = await client.call_tool(
            "catalog.match_qso",
            {
                **base,
                "catalog_filters": {
                    "conditions": [{"field": "active", "op": "eq", "value": False}]
                },
                "relation": "qso_only",
                "sort": [{"field": "qso_count", "direction": "desc"}],
            },
        )

    assert matched.data["items"] == [
        {"reference": "K-0001", "qso_count": 2},
        {"reference": "OK-0001", "qso_count": 1},
    ]
    assert matched.data["summary"] == {
        "catalog_values": 3,
        "matched_values": 2,
        "not_matched_values": 1,
        "qso_only_values": 1,
    }
    assert logging_side.data["items"] == [
        {"reference": "K-0001"},
        {"reference": "OK-0001"},
    ]
    assert logging_side.data["summary"]["qso_only_values"] == 0
    assert qso_only.data["items"] == [
        {"key": "K-0001", "qso_count": 2},
        {"key": "JA-0001", "qso_count": 1},
    ]


async def test_qso_only_ignores_empty_keys_and_returns_occurrence_count(
    catalog_match_database,
) -> None:
    async with Client(create_server(catalog_match_database)) as client:
        result = await client.call_tool(
            "catalog.match_qso",
            {
                "catalog": "wwff",
                "qso_field": "wwff_ref",
                "scope": {"station_scope": "all"},
                "relation": "qso_only",
            },
        )

    assert result.data["items"] == [{"key": "UNKNOWN-1", "qso_count": 2}]
    assert result.data["summary"] == {
        "catalog_values": 2,
        "matched_values": 1,
        "not_matched_values": 1,
        "qso_only_values": 1,
    }


async def test_catalog_filter_can_produce_an_empty_catalog_population(
    catalog_match_database,
) -> None:
    async with Client(create_server(catalog_match_database)) as client:
        result = await client.call_tool(
            "catalog.match_qso",
            {
                "catalog": "pota",
                "qso_field": "pota_ref",
                "scope": {"station_scope": "all"},
                "catalog_filters": {
                    "conditions": [
                        {"field": "reference", "op": "starts_with", "value": "ZZ-"}
                    ]
                },
                "relation": "qso_only",
            },
        )

    assert result.data["summary"] == {
        "catalog_values": 0,
        "matched_values": 0,
        "not_matched_values": 0,
        "qso_only_values": 3,
    }
    assert result.data["effective_scope"] == {"station_scope": "all"}


async def test_iota_matches_group_reference_and_rejects_island_id(
    catalog_match_database,
) -> None:
    async with Client(create_server(catalog_match_database)) as client:
        matched = await client.call_tool(
            "catalog.match_qso",
            {
                "catalog": "iota",
                "qso_field": "iota",
                "scope": {"station_scope": "all"},
                "relation": "matched",
                "fields": ["reference"],
            },
        )
        invalid = await client.call_tool(
            "catalog.match_qso",
            {
                "catalog": "iota",
                "qso_field": "iota_island_id",
                "scope": {"station_scope": "all"},
            },
            raise_on_error=False,
        )

    assert matched.data["items"] == [
        {"reference": "EU-001"},
        {"reference": "NA-026"},
    ]
    assert invalid.is_error is True
    assert "cannot be matched" in invalid.content[0].text


async def test_satellite_matches_names_and_reports_qso_only_names(
    catalog_match_database,
) -> None:
    base = {
        "catalog": "satellite",
        "qso_field": "satellite_name",
        "scope": {"station_scope": "all"},
        "qso_filters": {
            "conditions": [{"field": "propagation_mode", "op": "eq", "value": "SAT"}]
        },
    }
    async with Client(create_server(catalog_match_database)) as client:
        matched = await client.call_tool(
            "catalog.match_qso",
            {**base, "relation": "matched", "fields": ["name", "number"]},
        )
        not_matched = await client.call_tool(
            "catalog.match_qso",
            {**base, "relation": "not_matched", "fields": ["name"]},
        )
        qso_only = await client.call_tool(
            "catalog.match_qso",
            {**base, "relation": "qso_only", "sort": [{"field": "key"}]},
        )
        invalid = await client.call_tool(
            "catalog.match_qso",
            {**base, "qso_field": "satellite_mode"},
            raise_on_error=False,
        )

    assert matched.data["items"] == [{"name": "AO-91", "number": 43017}]
    assert not_matched.data["items"] == [{"name": "ISS"}, {"name": "RS-44"}]
    assert qso_only.data["items"] == [{"key": "SO-50", "qso_count": 1}]
    assert invalid.is_error is True
    assert "cannot be matched" in invalid.content[0].text


async def test_sota_match_returns_qso_statistics_with_catalog_facts(
    catalog_match_database,
) -> None:
    async with Client(create_server(catalog_match_database)) as client:
        matched = await client.call_tool(
            "catalog.match_qso",
            {
                "catalog": "sota",
                "qso_field": "sota_ref",
                "scope": {"station_scope": "all"},
                "relation": "matched",
                "fields": [
                    "reference",
                    "points",
                    "qso_count",
                    "first_qso",
                    "last_qso",
                ],
                "sort": [{"field": "qso_count", "direction": "desc"}],
            },
        )
        not_matched = await client.call_tool(
            "catalog.match_qso",
            {
                "catalog": "sota",
                "qso_field": "sota_ref",
                "scope": {"station_scope": "all"},
                "qso_filters": {
                    "conditions": [
                        {"field": "mode", "op": "eq", "value": "CW"}
                    ]
                },
                "relation": "not_matched",
                "fields": ["reference", "qso_count", "first_qso", "last_qso"],
            },
        )

    assert matched.data["items"] == [
        {
            "reference": "OK/PA-001",
            "points": 10,
            "qso_count": 1,
            "first_qso": "2025-12-31T23:59:00Z",
            "last_qso": "2025-12-31T23:59:00Z",
        },
        {
            "reference": "W1/AM-001",
            "points": 8,
            "qso_count": 1,
            "first_qso": "2026-01-15T12:30:00Z",
            "last_qso": "2026-01-15T12:30:00Z",
        },
    ]
    assert not_matched.data["items"] == [
        {
            "reference": "W1/AM-001",
            "qso_count": 0,
            "first_qso": None,
            "last_qso": None,
        }
    ]


async def test_rejects_invalid_mapping_and_unavailable_mapped_field(
    catalog_match_database, reduced_catalog_database
) -> None:
    async with Client(create_server(catalog_match_database)) as client:
        invalid = await client.call_tool(
            "catalog.match_qso",
            {
                "catalog": "pota",
                "qso_field": "dxcc",
                "scope": {"station_scope": "all"},
            },
            raise_on_error=False,
        )
    async with Client(create_server(reduced_catalog_database)) as client:
        unavailable = await client.call_tool(
            "catalog.match_qso",
            {
                "catalog": "pota",
                "qso_field": "my_pota_ref",
                "scope": {"station_scope": "all"},
            },
            raise_on_error=False,
        )

    assert invalid.is_error is True
    assert "cannot be matched" in invalid.content[0].text
    assert unavailable.is_error is True
    assert "unavailable" in unavailable.content[0].text


async def test_usage_log_keeps_match_values_private_and_records_one_sql(
    catalog_match_database, tmp_path
) -> None:
    usage_log = tmp_path / "usage.jsonl"
    async with Client(create_server(catalog_match_database, usage_log)) as client:
        await client.call_tool(
            "catalog.match_qso",
            {
                "catalog": "pota",
                "qso_field": "pota_ref",
                "scope": {
                    "station_scope": "callsign",
                    "station_callsigns": [{"callsign": "OK1MLG", "grid": "JO70AA"}],
                },
                "qso_filters": {
                    "conditions": [
                        {"field": "callsign", "op": "eq", "value": "JA1AAA"}
                    ]
                },
                "catalog_filters": {
                    "conditions": [
                        {"field": "name", "op": "eq", "value": "Yellowstone"}
                    ]
                },
                "relation": "matched",
                "fields": ["reference", "name"],
            },
        )

    raw = usage_log.read_text()
    event = json.loads(raw)
    for private_value in ("OK1MLG", "JO70AA", "JA1AAA", "Yellowstone", "K-0001"):
        assert private_value not in raw
    assert event["arguments"]["qso_filters"]["conditions"] == [
        {"field": "callsign", "op": "eq"}
    ]
    assert event["arguments"]["catalog_filters"]["conditions"] == [
        {"field": "name", "op": "eq"}
    ]
    assert len(event["sql"]) == 1
    assert event["sql"][0]["statement"].startswith("WITH RECURSIVE ")
    assert "Yellowstone" not in event["sql"][0]["statement"]
    assert '"_first_qso"' not in event["sql"][0]["statement"]
    assert '"_last_qso"' not in event["sql"][0]["statement"]
    assert event["result"]["set_counts"] == {
        "catalog_values": 2,
        "matched_values": 1,
        "not_matched_values": 1,
        "qso_only_values": 1,
    }


async def test_usage_log_summarizes_catalog_partitions(
    catalog_match_database, tmp_path
) -> None:
    usage_log = tmp_path / "partition-usage.jsonl"
    async with Client(create_server(catalog_match_database, usage_log)) as client:
        await client.call_tool(
            "catalog.match_qso",
            {
                "catalog": "dxcc",
                "qso_field": "dxcc",
                "scope": {"station_scope": "all"},
                "partition_by": ["band", "mode"],
                "relation": "matched",
            },
        )

    event = json.loads(usage_log.read_text())
    assert event["arguments"]["partition_by"] == ["band", "mode"]
    assert event["result"]["partitions"] == 3
    assert len(event["sql"]) == 1
