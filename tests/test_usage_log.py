"""Tests for privacy-conscious MCP usage logging."""

import json

from fastmcp import Client

from qlog_mcp.server import create_server


async def test_logs_query_and_aggregate_without_values(qlog_database, tmp_path) -> None:
    usage_log = tmp_path / "state" / "usage.jsonl"
    secret_callsign = "JA2BBB"
    list_value = "JA-0001"

    async with Client(create_server(qlog_database, usage_log)) as client:
        await client.call_tool(
            "qso.query",
            {
                "scope": {"station_scope": "all", "date_from": "2026-01-01"},
                "filters": {
                    "conditions": [
                        {"field": "callsign", "op": "eq", "value": secret_callsign},
                        {"field": "pota_ref", "op": "has", "value": list_value},
                    ]
                },
                "fields": ["id", "callsign"],
                "limit": 10,
            },
        )
        await client.call_tool(
            "qso.aggregate",
            {
                "scope": {"station_scope": "all"},
                "group_by": [
                    {"field": "pota_ref", "explode": True, "as": "park"},
                    {
                        "field": "datetime",
                        "interval": "minute",
                        "size": 10,
                        "as": "period",
                    },
                ],
                "one_per_group": {
                    "fields": ["pota_ref", "callsign"],
                    "keep": "first",
                },
                "metrics": [
                    {"function": "count", "as": "qso_count"},
                    {
                        "function": "distinct_count",
                        "fields": ["country", "band"],
                        "as": "country_bands",
                    },
                ],
            },
        )

    raw_log = usage_log.read_text()
    events = [json.loads(line) for line in raw_log.splitlines()]

    assert secret_callsign not in raw_log
    assert list_value not in raw_log
    assert "2026-01-01" not in raw_log
    assert [event["tool"] for event in events] == ["qso.query", "qso.aggregate"]
    assert {event["session_id"] for event in events} == {events[0]["session_id"]}
    assert [event["call_id"] for event in events] == ["000001", "000002"]

    query = events[0]
    assert query["outcome"] == "ok"
    assert query["tool_duration_ms"] >= query["sql"][0]["duration_ms"]
    assert query["arguments"] == {
        "scope": {
            "station_scope": "all",
            "station_callsign_count": 0,
            "operator_callsign_count": 0,
            "station_profile_count": 0,
            "has_date_from": True,
            "has_date_to": False,
        },
        "filters": {
            "group_count": 1,
            "conditions": [
                {"field": "callsign", "op": "eq"},
                {"field": "pota_ref", "op": "has"},
            ],
        },
        "fields": ["id", "callsign"],
        "one_per_group": None,
        "sort": None,
        "limit": 10,
        "offset": 0,
    }
    assert query["sql"][0]["statement"].startswith("SELECT ")
    assert query["sql"][0]["parameter_count"] == 5
    assert query["sql"][0]["parameter_types"] == ["str", "str", "str", "int", "int"]
    assert query["sql"][0]["returned_rows"] == 1
    assert query["result"]["rows"] == 1
    assert query["result"]["bytes"] > 0

    aggregate = events[1]
    assert aggregate["outcome"] == "ok"
    assert aggregate["arguments"]["group_by"] == [
        {"field": "pota_ref", "as": "park", "explode": True},
        {
            "field": "datetime",
            "as": "period",
            "interval": "minute",
            "has_size": True,
        },
    ]
    assert aggregate["arguments"]["one_per_group"] == {
        "fields": ["pota_ref", "callsign"],
        "keep": "first",
    }
    assert aggregate["arguments"]["metrics"] == [
        {"function": "count", "as": "qso_count"},
        {
            "function": "distinct_count",
            "as": "country_bands",
            "fields": ["country", "band"],
        },
    ]
    assert "GROUP BY" in aggregate["sql"][0]["statement"]
    assert "600" not in aggregate["sql"][0]["statement"]
    assert aggregate["sql"][0]["parameter_types"] == ["int"] * 5
    assert aggregate["sql"][0]["returned_rows"] == 2
    assert aggregate["result"]["rows"] == 2


async def test_logs_tool_errors_without_untrusted_argument_values(qlog_database, tmp_path) -> None:
    usage_log = tmp_path / "usage.jsonl"
    secret_field = "secret field value"

    async with Client(create_server(qlog_database, usage_log)) as client:
        result = await client.call_tool(
            "qso.query",
            {
                "scope": {"station_scope": "all"},
                "fields": [secret_field],
            },
            raise_on_error=False,
        )

    event = json.loads(usage_log.read_text())
    assert result.is_error is True
    assert secret_field not in usage_log.read_text()
    assert event["outcome"] == "error"
    assert event["error_type"] == "InvalidQueryError"
    assert event["arguments"]["fields"] == ["<invalid>"]
    assert event["sql"] == []
