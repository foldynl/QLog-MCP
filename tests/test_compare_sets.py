"""Tests for server-side comparisons between two QSO value sets."""

from fastmcp import Client

from qlog_mcp.server import create_server


def _band_set(band: str, *, key: str = "dxcc") -> dict:
    return {
        "scope": {"station_scope": "all"},
        "key": key,
        "filters": {"conditions": [{"field": "band", "op": "eq", "value": band}]},
    }


async def test_compares_two_filtered_qso_populations(qlog_database) -> None:
    arguments = {
        "left": _band_set("20m"),
        "right": _band_set("15m"),
        "relation": "left_only",
    }
    async with Client(create_server(qlog_database)) as client:
        left_only = await client.call_tool("qso.compare_sets", arguments)
        arguments["relation"] = "both"
        both = await client.call_tool("qso.compare_sets", arguments)

    assert left_only.data == {
        "relation": "left_only",
        "left": {"key": "dxcc", "effective_scope": {"station_scope": "all"}},
        "right": {"key": "dxcc", "effective_scope": {"station_scope": "all"}},
        "summary": {
            "left_values": 2,
            "right_values": 1,
            "both_values": 1,
            "left_only_values": 1,
            "right_only_values": 0,
            "either_values": 2,
        },
        "items": [
            {
                "key": 291,
                "left_qso_count": 1,
                "right_qso_count": 0,
                "left_first_qso": "2026-02-21T09:00:00Z",
                "left_last_qso": "2026-02-21T09:00:00Z",
                "right_first_qso": None,
                "right_last_qso": None,
            }
        ],
        "page": {
            "limit": 100,
            "offset": 0,
            "returned": 1,
            "has_more": False,
            "next_offset": None,
        },
    }
    assert both.data["items"] == [
        {
            "key": 339,
            "left_qso_count": 2,
            "right_qso_count": 1,
            "left_first_qso": "2025-12-31T23:59:00Z",
            "left_last_qso": "2026-02-20T08:00:00Z",
            "right_first_qso": "2026-01-15T12:30:00Z",
            "right_last_qso": "2026-01-15T12:30:00Z",
        }
    ]


async def test_supports_separate_scopes_sorting_and_pagination(qlog_database) -> None:
    arguments = {
        "left": {
            "scope": {
                "station_scope": "callsign",
                "station_callsigns": [{"callsign": "OK1MLG", "grid": "JO70AA"}],
            },
            "key": "callsign",
        },
        "right": {
            "scope": {
                "station_scope": "callsign",
                "station_callsigns": [{"callsign": "OK1MLG", "grid": "JO80BB"}],
            },
            "key": "callsign",
        },
        "relation": "either",
        "sort": [{"field": "key", "direction": "desc"}],
        "limit": 2,
    }
    async with Client(create_server(qlog_database)) as client:
        first = await client.call_tool("qso.compare_sets", arguments)
        arguments["offset"] = first.data["page"]["next_offset"]
        second = await client.call_tool("qso.compare_sets", arguments)

    assert first.data["summary"] == {
        "left_values": 3,
        "right_values": 1,
        "both_values": 0,
        "left_only_values": 3,
        "right_only_values": 1,
        "either_values": 4,
    }
    assert [item["key"] for item in first.data["items"]] == ["K1ABC", "JA3CCC"]
    assert first.data["page"]["next_offset"] == 2
    assert [item["key"] for item in second.data["items"]] == ["JA2BBB", "JA1AAA"]
    assert second.data["page"]["has_more"] is False


async def test_compares_paired_list_keys_as_normalized_items(
    catalog_match_database,
) -> None:
    async with Client(create_server(catalog_match_database)) as client:
        result = await client.call_tool(
            "qso.compare_sets",
            {
                "left": {"scope": {"station_scope": "all"}, "key": "pota_ref"},
                "right": {
                    "scope": {"station_scope": "all"},
                    "key": "my_pota_ref",
                },
                "relation": "left_only",
            },
        )

    assert result.data["summary"] == {
        "left_values": 3,
        "right_values": 2,
        "both_values": 2,
        "left_only_values": 1,
        "right_only_values": 0,
        "either_values": 3,
    }
    assert result.data["items"] == [
        {
            "key": "JA-0001",
            "left_qso_count": 1,
            "right_qso_count": 0,
            "left_first_qso": "2026-01-15T12:30:00Z",
            "left_last_qso": "2026-01-15T12:30:00Z",
            "right_first_qso": None,
            "right_last_qso": None,
        }
    ]


async def test_empty_side_still_returns_complete_summary(qlog_database) -> None:
    async with Client(create_server(qlog_database)) as client:
        result = await client.call_tool(
            "qso.compare_sets",
            {
                "left": _band_set("20m"),
                "right": _band_set("40m"),
                "relation": "both",
            },
        )
        both_empty = await client.call_tool(
            "qso.compare_sets",
            {
                "left": _band_set("40m"),
                "right": _band_set("40m"),
                "relation": "either",
            },
        )

    assert result.data["summary"]["left_values"] == 2
    assert result.data["summary"]["right_values"] == 0
    assert result.data["summary"]["both_values"] == 0
    assert result.data["items"] == []
    assert both_empty.data["summary"] == {
        "left_values": 0,
        "right_values": 0,
        "both_values": 0,
        "left_only_values": 0,
        "right_only_values": 0,
        "either_values": 0,
    }
    assert both_empty.data["items"] == []


async def test_rejects_unknown_incompatible_and_unavailable_keys(qlog_database) -> None:
    async with Client(create_server(qlog_database)) as client:
        unknown = await client.call_tool(
            "qso.compare_sets",
            {
                "left": _band_set("20m", key="missing"),
                "right": _band_set("15m", key="missing"),
                "relation": "both",
            },
            raise_on_error=False,
        )
        incompatible = await client.call_tool(
            "qso.compare_sets",
            {
                "left": _band_set("20m", key="country"),
                "right": _band_set("15m", key="callsign"),
                "relation": "both",
            },
            raise_on_error=False,
        )
        unavailable = await client.call_tool(
            "qso.compare_sets",
            {
                "left": _band_set("20m", key="my_dxcc"),
                "right": _band_set("15m", key="my_dxcc"),
                "relation": "both",
            },
            raise_on_error=False,
        )

    assert unknown.is_error is True
    assert "Unknown QSO set key" in unknown.content[0].text
    assert incompatible.is_error is True
    assert "Incompatible QSO set keys" in incompatible.content[0].text
    assert unavailable.is_error is True
    assert "unavailable" in unavailable.content[0].text
