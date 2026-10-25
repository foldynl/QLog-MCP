"""Semantic value validation through the public query and aggregation tools."""

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from qlog_mcp.database import Database
from qlog_mcp.errors import InvalidQueryError
from qlog_mcp.filters import FilterCondition, FilterGroup
from qlog_mcp.qso import LogScope, QsoQuery
from qlog_mcp.server import create_server


@pytest.mark.parametrize(
    "catalog,condition,expected",
    [
        (None, {"field": "frequency", "op": "gt", "value": True}, "finite JSON number"),
        (None, {"field": "frequency", "value": "14.025"}, "finite JSON number"),
        (None, {"field": "frequency", "op": "in", "value": [14.025, False]}, "finite JSON number"),
        (None, {"field": "dxcc", "op": "between", "value": [1, "339"]}, "finite JSON number"),
        (None, {"field": "callsign", "value": 123}, "JSON string"),
        (None, {"field": "datetime", "value": "not-a-date"}, "ISO date/time"),
        (None, {"field": "datetime", "value": "2026-02-30"}, "ISO date/time"),
        (None, {"field": "datetime", "value": "2026-02-01T25:00:00Z"}, "ISO date/time"),
        (None, {"field": "datetime", "value": "2026-02-01T12:00:00+15:00"}, "ISO date/time"),
        (None, {"field": "datetime", "op": "in", "value": ["2026-01-01", "now"]}, "ISO date/time"),
        (
            None,
            {"field": "frequency", "op": "is_not_null", "value": "ignored"},
            "omit value or use null",
        ),
        ("dxcc", {"field": "code", "op": "gt", "value": True}, "finite JSON number"),
        ("dxcc", {"field": "code", "op": "in", "value": [339, "291"]}, "finite JSON number"),
        ("dxcc", {"field": "deleted", "value": 1}, "JSON boolean"),
        ("dxcc", {"field": "deleted", "op": "in", "value": [True, 0]}, "JSON boolean"),
        ("dxcc", {"field": "name", "value": False}, "JSON string"),
        ("dxcc", {"field": "code", "op": "is_null", "value": 0}, "omit value or use null"),
        ("sota", {"field": "valid_from", "value": "2026-02-30"}, "YYYY-MM-DD"),
        ("sota", {"field": "valid_from", "value": "20260201"}, "YYYY-MM-DD"),
        (
            "sota",
            {"field": "valid_from", "op": "between", "value": ["2007-01-01", "not-a-date"]},
            "YYYY-MM-DD",
        ),
    ],
)
async def test_rejects_filter_values_inconsistent_with_field_type(
    catalog_qlog_database: Path,
    tmp_path: Path,
    catalog: str | None,
    condition: dict[str, Any],
    expected: str,
) -> None:
    arguments = {"filters": {"conditions": [condition]}}
    if catalog is None:
        tool = "qso.query"
        arguments["scope"] = {"station_scope": "all"}
    else:
        tool = "catalog.query"
        arguments["catalog"] = catalog
    usage_log = tmp_path / "usage.jsonl"
    async with Client(create_server(catalog_qlog_database, usage_log)) as client:
        result = await client.call_tool(tool, arguments, raise_on_error=False)

    assert result.is_error is True
    message = result.content[0].text
    assert condition["field"] in message
    assert condition.get("op", "eq") in message
    assert expected in message
    event = json.loads(usage_log.read_text())
    assert event["sql"] == []
    assert "not-a-date" not in usage_log.read_text()
    assert "ignored" not in usage_log.read_text()


@pytest.mark.parametrize("value", [float("inf"), -float("inf"), float("nan"), 2**64])
async def test_rejects_numbers_sqlite_cannot_safely_bind(
    qlog_database: Path, value: float
) -> None:
    # Non-finite values are not JSON numbers; exercise the same compiler directly.
    query = QsoQuery(Database(qlog_database))
    filters = FilterGroup(conditions=[FilterCondition(field="frequency", op="gt", value=value)])
    with pytest.raises(InvalidQueryError, match="finite JSON number"):
        await query.query(LogScope(station_scope="all"), filters, ["id"], None, None, 100, 0)


@pytest.mark.parametrize("with_calculations", [False, True])
@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("qsos", "1", "finite JSON number"),
        ("qsos", True, "finite JSON number"),
        ("first", "not-a-date", "ISO date/time"),
        ("first", 1, "ISO date/time"),
    ],
)
async def test_having_validates_metric_result_type(
    qlog_database: Path, with_calculations: bool, field: str, value: Any, expected: str
) -> None:
    arguments = {
        "scope": {"station_scope": "all"},
        "group_by": ["band"],
        "metrics": [
            {"function": "count", "as": "qsos"},
            {"function": "min", "field": "datetime", "as": "first"},
        ],
        "having": [{"field": field, "op": "gt", "value": value}],
    }
    if with_calculations:
        arguments["calculations"] = [{"op": "divide", "left": "qsos", "right": 2, "as": "half"}]
    async with Client(create_server(qlog_database)) as client:
        result = await client.call_tool("qso.aggregate", arguments, raise_on_error=False)

    assert result.is_error is True
    assert field in result.content[0].text
    assert expected in result.content[0].text


@pytest.mark.parametrize("with_calculations", [False, True])
@pytest.mark.parametrize(
    "value",
    ["2025-12-31T23:59:00Z", "2025-12-31 23:59:00", "2026-01-01T00:59:00+01:00"],
)
async def test_having_compares_equivalent_timestamps_as_instants(
    qlog_database: Path, with_calculations: bool, value: str
) -> None:
    arguments = {
        "scope": {"station_scope": "all"},
        "group_by": [],
        "metrics": [{"function": "min", "field": "datetime", "as": "first"}],
        "having": [{"field": "first", "value": value}],
    }
    if with_calculations:
        arguments["calculations"] = [{"op": "add", "left": 1, "right": 2, "as": "three"}]
    async with Client(create_server(qlog_database)) as client:
        result = await client.call_tool("qso.aggregate", arguments)

    assert len(result.data["rows"]) == 1
    assert result.data["rows"][0]["first"] == "2025-12-31T23:59:00Z"


@pytest.mark.parametrize(
    "op,value,matched",
    [
        ("eq", "2025-12-31T23:59:00.500Z", False),
        ("gte", "2025-12-31T23:59:00.500Z", False),
        ("lt", "2025-12-31T23:59:00.500Z", True),
    ],
)
async def test_having_does_not_merge_distinct_fractional_timestamps(
    qlog_database: Path, op: str, value: str, matched: bool
) -> None:
    async with Client(create_server(qlog_database)) as client:
        result = await client.call_tool(
            "qso.aggregate",
            {
                "scope": {"station_scope": "all"},
                "group_by": [],
                "metrics": [{"function": "min", "field": "datetime", "as": "first"}],
                "having": [{"field": "first", "op": op, "value": value}],
            },
        )
    assert bool(result.data["rows"]) is matched


@pytest.mark.parametrize("function", ["count", "distinct_count", "sum", "avg", "min", "max"])
async def test_all_numeric_metric_functions_require_numeric_having(
    qlog_database: Path, function: str
) -> None:
    metric = {"function": function, "as": "metric"}
    if function != "count":
        metric["field"] = "frequency"
    async with Client(create_server(qlog_database)) as client:
        result = await client.call_tool(
            "qso.aggregate",
            {
                "scope": {"station_scope": "all"},
                "group_by": [],
                "metrics": [metric],
                "having": [{"field": "metric", "value": "1"}],
            },
            raise_on_error=False,
        )
    assert result.is_error
    assert "finite JSON number" in result.content[0].text


@pytest.mark.parametrize("function", ["min", "max"])
@pytest.mark.parametrize("value,invalid", [("2026-03-01", False), ("20260301", True)])
async def test_having_uses_date_field_type(
    qlog_database: Path, function: str, value: str, invalid: bool
) -> None:
    with sqlite3.connect(qlog_database) as connection:
        connection.execute("ALTER TABLE contacts ADD COLUMN qsl_rdate TEXT")
        connection.execute("UPDATE contacts SET qsl_rdate = '2026-03-01' WHERE id = 1")
    async with Client(create_server(qlog_database)) as client:
        result = await client.call_tool(
            "qso.aggregate",
            {
                "scope": {"station_scope": "all"},
                "group_by": [],
                "metrics": [{"function": function, "field": "qsl_received_date", "as": "date"}],
                "having": [{"field": "date", "value": value}],
            },
            raise_on_error=False,
        )
    assert result.is_error is invalid
    if invalid:
        assert "YYYY-MM-DD" in result.content[0].text
    else:
        assert result.data["rows"] == [{"date": "2026-03-01"}]


@pytest.mark.parametrize(
    "tool,arguments",
    [
        (
            "qso.query",
            {"filters": {"groups": [{"conditions": [{"field": "frequency", "value": True}]}]}},
        ),
        (
            "qso.aggregate",
            {
                "group_by": [],
                "metrics": [
                    {
                        "function": "count",
                        "as": "qsos",
                        "filters": {"conditions": [{"field": "frequency", "value": True}]},
                    }
                ],
            },
        ),
        (
            "catalog.match_qso",
            {
                "catalog": "dxcc",
                "qso_field": "dxcc",
                "qso_filters": {"conditions": [{"field": "frequency", "value": True}]},
            },
        ),
        (
            "catalog.match_qso",
            {
                "catalog": "dxcc",
                "qso_field": "dxcc",
                "catalog_filters": {"conditions": [{"field": "code", "value": True}]},
            },
        ),
        (
            "membership.match_qso",
            {
                "club": "OPEN",
                "qso_filters": {"conditions": [{"field": "frequency", "value": True}]},
            },
        ),
    ],
)
async def test_shared_filter_paths_validate_values(
    catalog_match_database: Path, tool: str, arguments: dict[str, Any]
) -> None:
    async with Client(create_server(catalog_match_database)) as client:
        result = await client.call_tool(
            tool, {"scope": {"station_scope": "all"}, **arguments}, raise_on_error=False
        )
    assert result.is_error
    assert "finite JSON number" in result.content[0].text


@pytest.mark.parametrize(
    "condition,ids",
    [
        ({"field": "frequency", "op": "gt", "value": 21}, [2]),
        ({"field": "dxcc", "op": "gt", "value": 338.5}, [1, 2, 3]),
        ({"field": "datetime", "op": "gte", "value": "2026-02-01"}, [3, 4]),
        ({"field": "datetime", "value": "2026-02-20T09:00:00+01:00"}, [3]),
        ({"field": "datetime", "value": "2026-02-20 08:00"}, [3]),
        ({"field": "frequency", "op": "is_not_null"}, [1, 2, 3, 4]),
        ({"field": "frequency", "op": "is_not_null", "value": None}, [1, 2, 3, 4]),
        ({"field": "pota_ref", "value": None}, [1]),
        ({"field": "pota_ref", "op": "neq", "value": None}, [2, 3, 4]),
        ({"field": "rst_sent", "value": "599"}, [1, 3]),
        ({"field": "extra_fields", "value": '{"app_qlog_test": "old"}'}, [1]),
    ],
)
async def test_valid_filter_values_preserve_results(
    qlog_database: Path, condition: dict[str, Any], ids: list[int]
) -> None:
    async with Client(create_server(qlog_database)) as client:
        result = await client.call_tool(
            "qso.query",
            {
                "scope": {"station_scope": "all"},
                "filters": {"conditions": [condition]},
                "fields": ["id"],
                "sort": [{"field": "id"}],
            },
        )
    assert [item["id"] for item in result.data["items"]] == ids


async def test_fractional_having_threshold_is_valid(qlog_database: Path) -> None:
    async with Client(create_server(qlog_database)) as client:
        result = await client.call_tool(
            "qso.aggregate",
            {
                "scope": {"station_scope": "all"},
                "group_by": ["band"],
                "metrics": [{"function": "count", "as": "qsos"}],
                "having": [{"field": "qsos", "op": "gt", "value": 1.5}],
            },
        )
    assert result.data["rows"] == [{"band": "20m", "qsos": 3}]


async def test_tool_descriptions_explain_semantic_value_validation() -> None:
    async with Client(create_server()) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    description = tools["qso.query"].input_schema["$defs"]["FilterCondition"]["properties"][
        "value"
    ]["description"]
    assert "qlog.get_schema" in description
    assert "finite JSON numbers" in description
    assert "YYYY-MM-DD" in description
    having = tools["qso.aggregate"].input_schema["$defs"]["AggregateHaving"]["properties"]["value"][
        "description"
    ]
    assert "result type" in having
    assert "numeric strings" in having
