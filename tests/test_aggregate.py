"""Tests for server-side QSO aggregation."""

import json

import pytest
from fastmcp import Client

from qlog_mcp.server import create_server


async def test_distinct_dxcc_by_band_in_date_scope(qlog_database) -> None:
    async with Client(create_server(qlog_database)) as client:
        result = await client.call_tool(
            "qso.aggregate",
            {
                "scope": {
                    "station_scope": "all",
                    "date_from": "2026-01-01",
                    "date_to": "2026-12-31",
                },
                "group_by": ["band"],
                "metrics": [
                    {
                        "function": "distinct_count",
                        "field": "dxcc",
                        "as": "dxcc_count",
                    }
                ],
                "order_by": [{"field": "dxcc_count", "direction": "desc"}],
            },
        )

    assert result.data == {
        "rows": [
            {"band": "20m", "dxcc_count": 2},
            {"band": "15m", "dxcc_count": 1},
        ],
        "group_by": ["band"],
        "metrics": ["dxcc_count"],
        "effective_scope": {
            "station_scope": "all",
            "date_from": "2026-01-01",
            "date_to": "2026-12-31",
        },
        "limit": 100,
        "returned": 2,
        "truncated": False,
    }


async def test_time_dimension_and_filtered_metric(qlog_database) -> None:
    async with Client(create_server(qlog_database)) as client:
        result = await client.call_tool(
            "qso.aggregate",
            {
                "scope": {"station_scope": "all", "date_from": "2025-01-01"},
                "group_by": ["year"],
                "metrics": [
                    {"function": "count", "as": "all_qsos"},
                    {
                        "function": "count",
                        "as": "asia_qsos",
                        "filters": {
                            "conditions": [
                                {"field": "continent", "op": "eq", "value": "AS"}
                            ]
                        },
                    },
                ],
                "order_by": [{"field": "year", "direction": "asc"}],
            },
        )

    assert result.data["rows"] == [
        {"year": "2025", "all_qsos": 1, "asia_qsos": 1},
        {"year": "2026", "all_qsos": 3, "asia_qsos": 2},
    ]


async def test_numeric_and_distinct_aggregate_functions(qlog_database) -> None:
    async with Client(create_server(qlog_database)) as client:
        result = await client.call_tool(
            "qso.aggregate",
            {
                "scope": {"station_scope": "all"},
                "group_by": [],
                "metrics": [
                    {"function": "count", "as": "qso_count"},
                    {
                        "function": "distinct_count",
                        "field": "pota_ref",
                        "as": "pota_count",
                    },
                    {"function": "sum", "field": "frequency", "as": "frequency_sum"},
                    {"function": "avg", "field": "frequency", "as": "frequency_avg"},
                    {"function": "min", "field": "frequency", "as": "frequency_min"},
                    {"function": "max", "field": "frequency", "as": "frequency_max"},
                    {"function": "min", "field": "datetime", "as": "first_qso"},
                    {"function": "max", "field": "datetime", "as": "last_qso"},
                ],
            },
        )

    row = result.data["rows"][0]
    assert row["qso_count"] == 4
    assert row["pota_count"] == 2
    assert row["frequency_sum"] == pytest.approx(63.384)
    assert row["frequency_avg"] == pytest.approx(15.846)
    assert row["frequency_min"] == pytest.approx(14.025)
    assert row["frequency_max"] == pytest.approx(21.074)
    assert row["first_qso"] == "2025-12-31T23:59:00Z"
    assert row["last_qso"] == "2026-02-21T09:00:00Z"


async def test_all_fields_can_group_and_limit_reports_truncation(qlog_database) -> None:
    async with Client(create_server(qlog_database)) as client:
        result = await client.call_tool(
            "qso.aggregate",
            {
                "scope": {"station_scope": "all"},
                "group_by": ["id"],
                "metrics": [{"function": "count", "as": "qso_count"}],
                "order_by": [{"field": "id", "direction": "asc"}],
                "limit": 2,
            },
        )

    assert result.data["rows"] == [
        {"id": 1, "qso_count": 1},
        {"id": 2, "qso_count": 1},
    ]
    assert result.data["truncated"] is True


async def test_having_filters_groups_by_metric_alias(qlog_database) -> None:
    async with Client(create_server(qlog_database)) as client:
        result = await client.call_tool(
            "qso.aggregate",
            {
                "scope": {"station_scope": "all"},
                "filters": {
                    "conditions": [{"field": "continent", "value": "AS"}]
                },
                "group_by": ["band"],
                "metrics": [
                    {
                        "function": "count",
                        "as": "cw_qsos",
                        "filters": {"conditions": [{"field": "mode", "value": "CW"}]},
                    }
                ],
                "having": [{"field": "cw_qsos", "op": "gt", "value": 1}],
            },
        )

    assert result.data["rows"] == [{"band": "20m", "cw_qsos": 2}]


async def test_having_rejects_unknown_metric_alias(qlog_database) -> None:
    async with Client(create_server(qlog_database)) as client:
        result = await client.call_tool(
            "qso.aggregate",
            {
                "scope": {"station_scope": "all"},
                "group_by": ["mode"],
                "metrics": [{"function": "count", "as": "qso_count"}],
                "having": [{"field": "other_count", "value": 1}],
            },
            raise_on_error=False,
        )

    assert result.is_error is True
    assert "Unknown aggregate having field: other_count" in result.content[0].text


async def test_calculates_filters_and_orders_confirmation_rate(qlog_database) -> None:
    async with Client(create_server(qlog_database)) as client:
        result = await client.call_tool(
            "qso.aggregate",
            {
                "scope": {"station_scope": "all"},
                "one_per_group": {"fields": ["callsign"], "keep": "first"},
                "group_by": ["band"],
                "metrics": [
                    {"function": "count", "as": "total"},
                    {
                        "function": "count",
                        "as": "confirmed",
                        "filters": {
                            "conditions": [
                                {"field": "lotw_received", "value": "Y"}
                            ]
                        },
                    },
                ],
                "calculations": [
                    {
                        "op": "percentage",
                        "left": "confirmed",
                        "right": "total",
                        "as": "confirmation_rate",
                    }
                ],
                "having": [
                    {"field": "total", "op": "gte", "value": 1},
                    {"field": "confirmation_rate", "op": "gt", "value": 30},
                ],
                "order_by": [
                    {"field": "confirmation_rate", "direction": "desc"}
                ],
                "limit": 1,
            },
        )

    assert result.data["rows"] == [
        {
            "band": "15m",
            "total": 1,
            "confirmed": 1,
            "confirmation_rate": 100.0,
        }
    ]
    assert result.data["metrics"] == ["total", "confirmed"]
    assert result.data["calculations"] == ["confirmation_rate"]
    assert result.data["truncated"] is True


async def test_supports_maximum_calculation_chain(qlog_database) -> None:
    calculations = []
    previous = "total"
    for index in range(20):
        output = f"calculation_{index}"
        calculations.append(
            {"op": "add", "left": previous, "right": previous, "as": output}
        )
        previous = output

    async with Client(create_server(qlog_database)) as client:
        result = await client.call_tool(
            "qso.aggregate",
            {
                "scope": {"station_scope": "all"},
                "group_by": [],
                "metrics": [{"function": "count", "as": "total"}],
                "calculations": calculations,
            },
        )

    row = result.data["rows"][0]
    assert row["total"] == 4
    assert row[previous] == 4 * 2**20
    assert result.data["calculations"] == [
        f"calculation_{index}" for index in range(20)
    ]


async def test_supports_all_calculation_operations_and_zero_denominator(
    qlog_database,
) -> None:
    async with Client(create_server(qlog_database)) as client:
        result = await client.call_tool(
            "qso.aggregate",
            {
                "scope": {"station_scope": "all"},
                "group_by": [],
                "metrics": [
                    {"function": "count", "as": "total"},
                    {
                        "function": "count",
                        "as": "confirmed",
                        "filters": {
                            "conditions": [{"field": "lotw_received", "value": "Y"}]
                        },
                    },
                    {
                        "function": "count",
                        "as": "zero",
                        "filters": {
                            "conditions": [{"field": "lotw_received", "value": "R"}]
                        },
                    },
                ],
                "calculations": [
                    {"op": "add", "left": "total", "right": "confirmed", "as": "added"},
                    {
                        "op": "subtract",
                        "left": "total",
                        "right": "confirmed",
                        "as": "subtracted",
                    },
                    {
                        "op": "multiply",
                        "left": "total",
                        "right": "confirmed",
                        "as": "multiplied",
                    },
                    {
                        "op": "divide",
                        "left": "total",
                        "right": "confirmed",
                        "as": "divided",
                    },
                    {
                        "op": "percentage",
                        "left": "confirmed",
                        "right": "total",
                        "as": "percentage",
                    },
                    {
                        "op": "divide",
                        "left": "total",
                        "right": "zero",
                        "as": "undefined",
                    },
                ],
            },
        )

    row = result.data["rows"][0]
    assert row["added"] == 6
    assert row["subtracted"] == 2
    assert row["multiplied"] == 8
    assert row["divided"] == 2.0
    assert row["percentage"] == 50.0
    assert row["undefined"] is None


async def test_chains_period_rates_and_orders_null_last(qlog_database) -> None:
    old_period = {
        "conditions": [
            {"field": "datetime", "op": "gte", "value": "2025-01-01"},
            {"field": "datetime", "op": "lt", "value": "2026-01-01"},
        ]
    }
    new_period = {
        "conditions": [
            {"field": "datetime", "op": "gte", "value": "2026-01-01"}
        ]
    }
    async with Client(create_server(qlog_database)) as client:
        result = await client.call_tool(
            "qso.aggregate",
            {
                "scope": {"station_scope": "all"},
                "group_by": ["band"],
                "metrics": [
                    {"function": "count", "as": "old_total", "filters": old_period},
                    {"function": "count", "as": "new_total", "filters": new_period},
                    {
                        "function": "count",
                        "as": "old_confirmed",
                        "filters": {
                            "conditions": [
                                *old_period["conditions"],
                                {"field": "lotw_received", "value": "Y"},
                            ]
                        },
                    },
                    {
                        "function": "count",
                        "as": "new_confirmed",
                        "filters": {
                            "conditions": [
                                *new_period["conditions"],
                                {"field": "lotw_received", "value": "Y"},
                            ]
                        },
                    },
                ],
                "calculations": [
                    {
                        "op": "percentage",
                        "left": "old_confirmed",
                        "right": "old_total",
                        "as": "old_rate",
                    },
                    {
                        "op": "percentage",
                        "left": "new_confirmed",
                        "right": "new_total",
                        "as": "new_rate",
                    },
                    {
                        "op": "subtract",
                        "left": "new_rate",
                        "right": "old_rate",
                        "as": "rate_change",
                    },
                ],
                "order_by": [{"field": "rate_change", "direction": "asc"}],
            },
        )

    assert [row["band"] for row in result.data["rows"]] == ["20m", "15m"]
    first, second = result.data["rows"]
    assert first["old_rate"] == 100.0
    assert first["new_rate"] == 0.0
    assert first["rate_change"] == -100.0
    assert second["old_rate"] is None
    assert second["rate_change"] is None


@pytest.mark.parametrize(
    ("metrics", "calculations", "having", "message"),
    [
        (
            [{"function": "count", "as": "total"}],
            [
                {
                    "op": "divide",
                    "left": "later",
                    "right": "total",
                    "as": "rate",
                },
                {"op": "add", "left": "total", "right": "total", "as": "later"},
            ],
            None,
            "Unknown or forward aggregate calculation operand: later",
        ),
        (
            [{"function": "min", "field": "datetime", "as": "first_qso"}],
            [
                {
                    "op": "add",
                    "left": "first_qso",
                    "right": "first_qso",
                    "as": "invalid",
                }
            ],
            None,
            "Aggregate calculation operand must be numeric: first_qso",
        ),
        (
            [{"function": "count", "as": "Total"}],
            [{"op": "add", "left": "Total", "right": "Total", "as": "total"}],
            None,
            "Duplicate aggregate result field: total",
        ),
        (
            [
                {"function": "count", "as": "Total"},
                {"function": "count", "as": "total"},
            ],
            [],
            None,
            "Duplicate aggregate result field: total",
        ),
        (
            [{"function": "count", "as": "total"}],
            [{"op": "divide", "left": "total", "right": "total", "as": "rate"}],
            [{"field": "rate", "value": "one"}],
            "Aggregate calculation having value must be numeric: rate",
        ),
    ],
)
async def test_rejects_invalid_aggregate_calculations(
    qlog_database, metrics, calculations, having, message
) -> None:
    async with Client(create_server(qlog_database)) as client:
        result = await client.call_tool(
            "qso.aggregate",
            {
                "scope": {"station_scope": "all"},
                "group_by": [],
                "metrics": metrics,
                "calculations": calculations,
                "having": having,
            },
            raise_on_error=False,
        )

    assert result.is_error is True
    assert message in result.content[0].text


@pytest.mark.parametrize(
    ("group_by", "expected"),
    [
        (
            {"field": "datetime", "interval": "week", "as": "week"},
            [
                {"week": "2025-12-29", "qso_count": 1},
                {"week": "2026-01-12", "qso_count": 1},
                {"week": "2026-02-16", "qso_count": 2},
            ],
        ),
        (
            {"field": "frequency", "bucket_size": 5, "as": "frequency_bucket"},
            [
                {"frequency_bucket": 10.0, "qso_count": 3},
                {"frequency_bucket": 20.0, "qso_count": 1},
            ],
        ),
    ],
)
async def test_group_by_bucket(qlog_database, group_by, expected) -> None:
    async with Client(create_server(qlog_database)) as client:
        result = await client.call_tool(
            "qso.aggregate",
            {
                "scope": {"station_scope": "all"},
                "group_by": [group_by],
                "metrics": [{"function": "count", "as": "qso_count"}],
                "order_by": [{"field": group_by["as"], "direction": "asc"}],
            },
        )

    assert result.data["group_by"] == [group_by["as"]]
    assert result.data["rows"] == expected


async def test_group_by_bucket_rejects_wrong_field_type(qlog_database) -> None:
    async with Client(create_server(qlog_database)) as client:
        result = await client.call_tool(
            "qso.aggregate",
            {
                "scope": {"station_scope": "all"},
                "group_by": [{"field": "country", "bucket_size": 10, "as": "country"}],
                "metrics": [{"function": "count", "as": "qso_count"}],
            },
            raise_on_error=False,
        )

    assert result.is_error is True
    assert "Numeric bucket requires an integer or number field" in result.content[0].text


async def test_can_group_by_optional_autovalue_field(qlog_database) -> None:
    async with Client(create_server(qlog_database)) as client:
        result = await client.call_tool(
            "qso.aggregate",
            {
                "scope": {"station_scope": "all"},
                "group_by": ["wavelog_upload_status"],
                "metrics": [{"function": "count", "as": "qso_count"}],
            },
        )

    assert result.data["rows"] == [
        {"wavelog_upload_status": None, "qso_count": 2},
        {"wavelog_upload_status": "N", "qso_count": 1},
        {"wavelog_upload_status": "Y", "qso_count": 1},
    ]


async def test_membership_fields_distinguish_qso_date_from_directory_snapshot(
    catalog_qlog_database,
) -> None:
    async with Client(create_server(catalog_qlog_database)) as client:
        schema = await client.call_tool("qlog.get_schema", {"domain": "qso"})
        rows = await client.call_tool(
            "qso.query",
            {
                "scope": {"station_scope": "all"},
                "fields": [
                    "id",
                    "member_clubs_at_qso_date",
                    "member_clubs_in_directory",
                ],
                "sort": [{"field": "id", "direction": "asc"}],
            },
        )
        dated = await client.call_tool(
            "qso.aggregate",
            {
                "scope": {"station_scope": "all"},
                "group_by": [
                    {
                        "field": "member_clubs_at_qso_date",
                        "explode": True,
                        "as": "club",
                    }
                ],
                "metrics": [{"function": "count", "as": "qsos"}],
                "order_by": [{"field": "club", "direction": "asc"}],
            },
        )
        malformed = await client.call_tool(
            "qso.aggregate",
            {
                "scope": {"station_scope": "all"},
                "filters": {
                    "conditions": [
                        {
                            "field": "member_clubs_at_qso_date",
                            "op": "has",
                            "value": "BROKEN",
                        }
                    ]
                },
                "group_by": [],
                "metrics": [{"function": "count", "as": "qsos"}],
            },
        )
        directory = await client.call_tool(
            "qso.aggregate",
            {
                "scope": {"station_scope": "all"},
                "filters": {
                    "conditions": [
                        {
                            "field": "member_clubs_in_directory",
                            "op": "has",
                            "value": "BROKEN",
                        }
                    ]
                },
                "group_by": [],
                "metrics": [{"function": "count", "as": "qsos"}],
            },
        )

    fields = schema.data["qso"]["fields"]
    assert fields["member_clubs_at_qso_date"]["cardinality"] == "many"
    assert "empty date bounds are unbounded" in fields["member_clubs_at_qso_date"]["description"]
    assert "downloaded into QLog" in fields["member_clubs_at_qso_date"]["description"]
    assert "award eligibility" in fields["member_clubs_at_qso_date"]["description"]
    assert "current validity" in fields["member_clubs_in_directory"]["description"]
    assert rows.data["items"] == [
        {"id": 1, "member_clubs_at_qso_date": "TIME", "member_clubs_in_directory": "TIME"},
        {"id": 2, "member_clubs_at_qso_date": "OPEN", "member_clubs_in_directory": "FUTURE,OPEN"},
        {
            "id": 3,
            "member_clubs_at_qso_date": "DAY",
            "member_clubs_in_directory": "AFTER,BROKEN,DAY",
        },
        {"id": 4, "member_clubs_at_qso_date": None, "member_clubs_in_directory": None},
    ]
    assert dated.data["rows"] == [
        {"club": "DAY", "qsos": 1},
        {"club": "OPEN", "qsos": 1},
        {"club": "TIME", "qsos": 1},
    ]
    assert malformed.data["rows"] == [{"qsos": 0}]
    assert directory.data["rows"] == [{"qsos": 1}]


@pytest.mark.parametrize(
    ("field", "club"),
    [
        ("member_clubs_at_qso_date", " time "),
        ("member_clubs_in_directory", " broken "),
    ],
)
async def test_membership_has_uses_direct_exists(
    catalog_qlog_database, tmp_path, field, club
) -> None:
    usage_log = tmp_path / "usage.jsonl"
    async with Client(create_server(catalog_qlog_database, usage_log)) as client:
        result = await client.call_tool(
            "qso.aggregate",
            {
                "scope": {"station_scope": "all"},
                "filters": {
                    "conditions": [{"field": field, "op": "has", "value": club}]
                },
                "group_by": [],
                "metrics": [{"function": "count", "as": "qsos"}],
            },
        )

    raw_log = usage_log.read_text()
    statement = json.loads(raw_log)["sql"][0]["statement"]
    assert result.data["rows"] == [{"qsos": 1}]
    assert club.strip().upper() not in raw_log
    assert "EXISTS (SELECT 1 FROM membership AS m WHERE" in statement
    assert "GROUP_CONCAT" not in statement
    assert "qlog_list_has" not in statement


async def test_rejects_aggregate_function_not_supported_by_field(qlog_database) -> None:
    async with Client(create_server(qlog_database)) as client:
        result = await client.call_tool(
            "qso.aggregate",
            {
                "scope": {"station_scope": "all"},
                "group_by": [],
                "metrics": [
                    {"function": "sum", "field": "country", "as": "country_sum"}
                ],
            },
            raise_on_error=False,
        )

    assert result.is_error is True
    assert "not supported for QSO field 'country'" in result.content[0].text


async def test_explodes_list_groups_and_distinct_metrics(list_qlog_database) -> None:
    async with Client(create_server(list_qlog_database)) as client:
        by_park = await client.call_tool(
            "qso.aggregate",
            {
                "scope": {"station_scope": "all"},
                "group_by": [{"field": "pota_ref", "explode": True, "as": "park"}],
                "metrics": [{"function": "count", "as": "qsos"}],
                "calculations": [
                    {"op": "add", "left": "qsos", "right": "qsos", "as": "double_qsos"}
                ],
                "order_by": [{"field": "park", "direction": "asc"}],
            },
        )
        totals = await client.call_tool(
            "qso.aggregate",
            {
                "scope": {"station_scope": "all"},
                "group_by": [],
                "metrics": [
                    {"function": "count", "as": "qsos"},
                    {"function": "avg", "field": "frequency", "as": "average_mhz"},
                    {
                        "function": "distinct_count",
                        "field": "pota_ref",
                        "explode": True,
                        "as": "parks",
                    },
                    {
                        "function": "distinct_count",
                        "field": "vucc_grids",
                        "explode": True,
                        "as": "grids",
                    },
                    {
                        "function": "distinct_count",
                        "field": "usaca_counties",
                        "explode": True,
                        "as": "counties",
                    },
                    {
                        "function": "distinct_count",
                        "field": "award_granted",
                        "explode": True,
                        "as": "awards",
                    },
                ],
            },
        )
        credits = await client.call_tool(
            "qso.aggregate",
            {
                "scope": {"station_scope": "all"},
                "group_by": [
                    {"field": "credit_granted", "explode": True, "as": "credit"}
                ],
                "metrics": [{"function": "count", "as": "qsos"}],
                "order_by": [{"field": "credit", "direction": "asc"}],
            },
        )
        subdivisions = await client.call_tool(
            "qso.aggregate",
            {
                "scope": {"station_scope": "all"},
                "group_by": [
                    {"field": "county_alt", "explode": True, "as": "subdivision"}
                ],
                "metrics": [{"function": "count", "as": "qsos"}],
                "order_by": [{"field": "subdivision", "direction": "asc"}],
            },
        )

    assert by_park.data["rows"] == [
        {"park": "JA-0001", "qsos": 1, "double_qsos": 2},
        {"park": "K-0001", "qsos": 2, "double_qsos": 4},
        {"park": "K-1000;K-2000", "qsos": 1, "double_qsos": 2},
        {"park": "K-4562", "qsos": 1, "double_qsos": 2},
    ]
    assert totals.data["rows"] == [
        {
            "qsos": 4,
            "average_mhz": pytest.approx(15.846),
            "parks": 4,
            "grids": 5,
            "counties": 2,
            "awards": 1,
        }
    ]
    assert credits.data["rows"] == [
        {"credit": "DXCC", "qsos": 1},
        {"credit": "LEGACY-AWARD", "qsos": 1},
        {"credit": "UNKNOWN", "qsos": 1},
    ]
    assert subdivisions.data["rows"] == [
        {"subdivision": "BROKEN", "qsos": 1},
        {"subdivision": "NZ_REGIONS:HAWKES BAY", "qsos": 1},
        {"subdivision": "NZ_REGIONS:WAIROA", "qsos": 1},
    ]


@pytest.mark.parametrize(
    ("group_by", "metrics", "message"),
    [
        (
            [{"field": "country", "explode": True, "as": "country"}],
            [{"function": "count", "as": "qsos"}],
            "list-valued",
        ),
        (
            [],
            [
                {
                    "function": "distinct_count",
                    "field": "country",
                    "explode": True,
                    "as": "countries",
                }
            ],
            "list-valued",
        ),
    ],
)
async def test_explode_rejects_scalar_fields(
    list_qlog_database, group_by, metrics, message
) -> None:
    async with Client(create_server(list_qlog_database)) as client:
        result = await client.call_tool(
            "qso.aggregate",
            {
                "scope": {"station_scope": "all"},
                "group_by": group_by,
                "metrics": metrics,
            },
            raise_on_error=False,
        )

    assert result.is_error is True
    assert message in result.content[0].text


async def test_aggregate_keeps_first_or_last_qso_before_metrics(
    analytics_qlog_database,
) -> None:
    arguments = {
        "scope": {"station_scope": "all"},
        "filters": {"conditions": [{"field": "continent", "value": "AS"}]},
        "one_per_group": {
            "fields": ["dxcc", "band", "mode"],
            "keep": "first",
        },
        "group_by": ["band"],
        "metrics": [
            {"function": "count", "as": "qsos"},
            {
                "function": "count",
                "as": "paper_confirmed",
                "filters": {
                    "conditions": [{"field": "qsl_received", "value": "Y"}]
                },
            },
        ],
        "order_by": [{"field": "band", "direction": "asc"}],
    }
    async with Client(create_server(analytics_qlog_database)) as client:
        first = await client.call_tool("qso.aggregate", arguments)
        arguments["one_per_group"]["keep"] = "last"
        last = await client.call_tool("qso.aggregate", arguments)
        arguments["one_per_group"] = {"fields": ["dxcc"], "keep": "first"}
        arguments["group_by"] = []
        arguments["order_by"] = None
        by_entity = await client.call_tool("qso.aggregate", arguments)

    assert first.data["rows"] == [
        {"band": "15m", "qsos": 1, "paper_confirmed": 0},
        {"band": "20m", "qsos": 1, "paper_confirmed": 0},
    ]
    assert last.data["rows"] == [
        {"band": "15m", "qsos": 1, "paper_confirmed": 0},
        {"band": "20m", "qsos": 1, "paper_confirmed": 1},
    ]
    assert by_entity.data["rows"] == [{"qsos": 1, "paper_confirmed": 0}]


async def test_list_expansion_precedes_aggregate_one_per_group(
    analytics_qlog_database,
) -> None:
    async with Client(create_server(analytics_qlog_database)) as client:
        result = await client.call_tool(
            "qso.aggregate",
            {
                "scope": {"station_scope": "all"},
                "filters": {"conditions": [{"field": "continent", "value": "AS"}]},
                "one_per_group": {
                    "fields": ["pota_ref", "callsign"],
                    "keep": "first",
                },
                "group_by": [
                    {"field": "pota_ref", "explode": True, "as": "park"}
                ],
                "metrics": [{"function": "count", "as": "qsos"}],
                "order_by": [{"field": "park", "direction": "asc"}],
            },
        )

    assert result.data["rows"] == [
        {"park": "JA-0001", "qsos": 1},
        {"park": "K-0001", "qsos": 1},
        {"park": "K-1000;K-2000", "qsos": 1},
        {"park": "K-4562", "qsos": 1},
        {"park": "K-9999", "qsos": 1},
    ]


async def test_composite_distinct_count_uses_typed_nonempty_tuples(
    analytics_qlog_database,
) -> None:
    async with Client(create_server(analytics_qlog_database)) as client:
        result = await client.call_tool(
            "qso.aggregate",
            {
                "scope": {"station_scope": "all"},
                "group_by": [],
                "metrics": [
                    {
                        "function": "distinct_count",
                        "fields": ["country", "band"],
                        "as": "country_bands",
                    },
                    {
                        "function": "distinct_count",
                        "fields": ["dxcc", "country"],
                        "as": "entity_names",
                    },
                ],
            },
        )

    assert result.data["rows"] == [{"country_bands": 6, "entity_names": 7}]


async def test_minute_buckets_align_to_utc_clock_boundaries(
    analytics_qlog_database,
) -> None:
    async with Client(create_server(analytics_qlog_database)) as client:
        result = await client.call_tool(
            "qso.aggregate",
            {
                "scope": {"station_scope": "all"},
                "filters": {
                    "conditions": [
                        {"field": "callsign", "op": "starts_with", "value": "X"}
                    ]
                },
                "group_by": [
                    {
                        "field": "datetime",
                        "interval": "minute",
                        "size": 10,
                        "as": "period",
                    }
                ],
                "metrics": [{"function": "count", "as": "qsos"}],
                "order_by": [{"field": "period", "direction": "asc"}],
            },
        )

    assert result.data["rows"] == [
        {"period": "2026-03-01T23:50:00Z", "qsos": 1},
        {"period": "2026-03-02T00:00:00Z", "qsos": 2},
        {"period": "2026-03-02T00:10:00Z", "qsos": 1},
        {"period": "2026-03-02T00:50:00Z", "qsos": 1},
        {"period": "2026-03-02T01:00:00Z", "qsos": 1},
    ]


@pytest.mark.parametrize(
    ("metric", "message"),
    [
        (
            {"function": "distinct_count", "as": "value"},
            "exactly one of field or fields",
        ),
        (
            {
                "function": "distinct_count",
                "field": "dxcc",
                "fields": ["band"],
                "as": "value",
            },
            "exactly one of field or fields",
        ),
        (
            {"function": "sum", "fields": ["dxcc", "band"], "as": "value"},
            "requires field and does not accept fields",
        ),
        (
            {"function": "distinct_count", "fields": ["pota_ref"], "as": "value"},
            "requires scalar QSO fields",
        ),
        (
            {
                "function": "distinct_count",
                "fields": ["dxcc"] * 11,
                "as": "value",
            },
            "at most 10 items",
        ),
    ],
)
async def test_rejects_invalid_composite_distinct_count(
    analytics_qlog_database, metric, message
) -> None:
    async with Client(create_server(analytics_qlog_database)) as client:
        result = await client.call_tool(
            "qso.aggregate",
            {
                "scope": {"station_scope": "all"},
                "group_by": [],
                "metrics": [metric],
            },
            raise_on_error=False,
        )

    assert result.is_error is True
    assert message in result.content[0].text


@pytest.mark.parametrize(
    ("group", "message"),
    [
        (
            {"field": "datetime", "interval": "minute", "as": "period"},
            "interval=minute requires size",
        ),
        (
            {
                "field": "datetime",
                "interval": "hour",
                "size": 10,
                "as": "period",
            },
            "size is valid only with interval=minute",
        ),
        (
            {
                "field": "datetime",
                "interval": "minute",
                "size": 61,
                "as": "period",
            },
            "less than or equal to 60",
        ),
    ],
)
async def test_rejects_invalid_minute_bucket(qlog_database, group, message) -> None:
    async with Client(create_server(qlog_database)) as client:
        result = await client.call_tool(
            "qso.aggregate",
            {
                "scope": {"station_scope": "all"},
                "group_by": [group],
                "metrics": [{"function": "count", "as": "qsos"}],
            },
            raise_on_error=False,
        )

    assert result.is_error is True
    assert message in result.content[0].text
