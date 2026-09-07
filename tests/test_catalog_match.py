"""Tests for neutral catalog/QSO set comparisons."""

import json

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
        "fields": ["reference", "name"],
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
            {**base, "relation": "qso_only", "fields": ["key", "qso_count"]},
        )

    expected_summary = {
        "catalog_values": 3,
        "matched_values": 0,
        "not_matched_values": 3,
        "qso_only_values": 0,
    }
    assert matched.data["items"] == []
    assert first.data["summary"] == second.data["summary"] == expected_summary
    assert first.data["items"] == [{"reference": "K-0001", "name": "Yellowstone"}]
    assert first.data["page"]["has_more"] is True
    assert second.data["items"] == [{"reference": "K-0002", "name": "Yellowstone"}]
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
                "fields": ["reference"],
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
        {"reference": "K-0001"},
        {"reference": "OK-0001"},
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


async def test_sota_aggregate_can_feed_catalog_enrichment(
    catalog_match_database,
) -> None:
    async with Client(create_server(catalog_match_database)) as client:
        counts = await client.call_tool(
            "qso.aggregate",
            {
                "scope": {"station_scope": "all"},
                "filters": {
                    "conditions": [{"field": "sota_ref", "op": "is_not_empty"}]
                },
                "group_by": ["sota_ref"],
                "metrics": [{"function": "count", "as": "qso_count"}],
            },
        )
        references = [row["sota_ref"] for row in counts.data["rows"]]
        summits = await client.call_tool(
            "catalog.query",
            {
                "catalog": "sota",
                "filters": {
                    "conditions": [
                        {"field": "reference", "op": "in", "value": references}
                    ]
                },
                "fields": ["reference", "points", "valid_from", "valid_to"],
            },
        )

    assert set(references) == {"OK/PA-001", "W1/AM-001"}
    assert {item["reference"] for item in summits.data["items"]} == set(references)


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
    assert event["result"]["set_counts"] == {
        "catalog_values": 2,
        "matched_values": 1,
        "not_matched_values": 1,
        "qso_only_values": 1,
    }
