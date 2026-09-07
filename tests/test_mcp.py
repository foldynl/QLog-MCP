"""Tests for the public MCP surface."""

import sqlite3

import pytest
from fastmcp import Client

from qlog_mcp import __main__ as cli
from qlog_mcp.server import create_server

EXPECTED_TOOLS = {
    "catalog.match_qso",
    "catalog.query",
    "qlog.get_context",
    "qlog.get_capabilities",
    "qlog.get_schema",
    "qso.aggregate",
    "qso.query",
}


def test_cli_starts_server_with_database(monkeypatch, tmp_path) -> None:
    database = tmp_path / "qlog.db"
    usage_log = tmp_path / "usage.jsonl"
    started_with = {}

    class Server:
        def run(self) -> None:
            started_with["ran"] = True

    def fake_create_server(database_path, usage_log_path):
        started_with["database"] = database_path
        started_with["usage_log"] = usage_log_path
        return Server()

    monkeypatch.setattr(cli, "create_server", fake_create_server)

    cli.main(["--database", str(database), "--usage-log", str(usage_log)])

    assert started_with == {"database": database, "usage_log": usage_log, "ran": True}


async def test_server_registers_public_tool_surface() -> None:
    tools = await create_server().list_tools()
    assert {tool.name for tool in tools} == EXPECTED_TOOLS


async def test_public_tool_schemas_are_described() -> None:
    server = create_server()
    tools = {tool.name: tool for tool in await server.list_tools()}
    query = tools["qso.query"]
    aggregate = tools["qso.aggregate"]
    catalog = tools["catalog.query"]
    catalog_match = tools["catalog.match_qso"]

    assert all(tool.description for tool in tools.values())
    assert all(tool.output_schema.get("description") for tool in tools.values())
    for tool in tools.values():
        assert all(
            definition.get("description")
            and all(
                property_schema.get("description")
                for property_schema in definition.get("properties", {}).values()
            )
            for definition in tool.parameters.get("$defs", {}).values()
        )
    assert "next_offset" in query.output_schema["description"]
    assert "next_offset" in catalog.output_schema["description"]
    assert "distinct non-empty keys" in catalog_match.output_schema["description"]
    assert "Only qso_count is a QSO occurrence count" in catalog_match.output_schema[
        "description"
    ]
    assert "truncated=true" in aggregate.output_schema["description"]
    assert all(
        parameter.get("description")
        for parameter in query.parameters["properties"].values()
    )
    assert query.parameters["properties"]["limit"]["minimum"] == 1
    assert query.parameters["properties"]["limit"]["maximum"] == 1000
    assert tools["qlog.get_schema"].parameters["properties"]["domain"]["description"]
    assert "once per needed domain" in tools["qlog.get_schema"].description
    assert "Do not call it before every operation" in server.instructions
    assert set(tools["qlog.get_schema"].parameters["properties"]["domain"]["enum"]) == {
        "qso",
        "catalog",
    }
    assert all(
        parameter.get("description")
        for parameter in catalog.parameters["properties"].values()
    )
    assert catalog.parameters["properties"]["limit"]["minimum"] == 1
    assert catalog.parameters["properties"]["limit"]["maximum"] == 1000
    assert all(
        parameter.get("description")
        for parameter in catalog_match.parameters["properties"].values()
    )
    assert "scope" in catalog_match.parameters["required"]
    assert catalog_match.parameters["properties"]["limit"]["minimum"] == 1
    assert catalog_match.parameters["properties"]["limit"]["maximum"] == 1000
    assert set(catalog_match.parameters["$defs"]["MatchRelation"]["enum"]) == {
        "matched",
        "not_matched",
        "qso_only",
    }
    assert "side and paired_field" in catalog_match.parameters["properties"][
        "qso_field"
    ]["description"]
    assert all(
        parameter.get("description")
        for parameter in aggregate.parameters["properties"].values()
    )
    assert aggregate.parameters["properties"]["metrics"]["minItems"] == 1
    assert "requested list expansion" in aggregate.parameters["properties"][
        "one_per_group"
    ]["description"]
    group = aggregate.parameters["$defs"]["GroupBySpec"]["properties"]
    assert "cardinality=many" in group["explode"]["description"]
    assert "interval" in group["field"]["description"]
    assert group["size"]["anyOf"][0]["minimum"] == 1
    assert group["size"]["anyOf"][0]["maximum"] == 60
    assert "minute" in aggregate.parameters["$defs"]["GroupInterval"]["enum"]
    metric = aggregate.parameters["$defs"]["AggregateMetric"]["properties"]
    assert "complete stored strings" in metric["explode"]["description"]
    assert metric["fields"]["anyOf"][0]["minItems"] == 1
    assert metric["fields"]["anyOf"][0]["maxItems"] == 10


async def test_capabilities_tool_through_mcp_client() -> None:
    async with Client(create_server()) as client:
        result = await client.call_tool("qlog.get_capabilities")

    assert result.is_error is False
    assert result.data["stage"] == "qso-query"
    assert result.data["qso"]["query"] is True
    assert result.data["qso"]["aggregate"] is True
    assert result.data["catalog"] == {
        "query": True,
        "match_qso": True,
        "names": ["pota", "sota", "wwff", "iota", "dxcc"],
    }


async def test_qlog_context_and_qso_schema(qlog_database) -> None:
    async with Client(create_server(qlog_database)) as client:
        context = await client.call_tool("qlog.get_context")
        schema = await client.call_tool("qlog.get_schema", {"domain": "qso"})

    assert context.data["database_schema"] == 40
    assert context.data["qso_count"] == 4
    assert context.data["station_callsigns"] == [
        {"callsign": "OK1MLG", "grid": "JO70AA"},
        {"callsign": "OK1MLG", "grid": "JO80BB"},
    ]
    assert context.data["station_profiles"][0]["name"] == "Home"
    assert schema.data["qso"]["fields"]["datetime"]["type"] == "datetime"
    assert schema.data["qso"]["fields"]["base_callsign"]["type"] == "string"
    assert schema.data["qso"]["fields"]["wavelog_upload_date"]["type"] == "date"
    assert schema.data["qso"]["fields"]["wavelog_upload_status"]["enum"] == {
        "name": "ADIF QSO Upload Status",
        "values": {
            "Y": "Uploaded and accepted by the online service",
            "N": "Do not upload to the online service",
            "M": "Modified after a previous upload",
        },
    }
    assert "contains" in schema.data["qso"]["fields"]["callsign"]["operators"]
    assert "ends_with" in schema.data["qso"]["fields"]["callsign"]["operators"]
    assert "contains" not in schema.data["qso"]["fields"]["dxcc"]["operators"]
    assert "is_empty" in schema.data["qso"]["fields"]["dxcc"]["operators"]
    assert schema.data["qso"]["fields"]["dxcc"]["aggregate_functions"] == [
        "distinct_count",
        "sum",
        "avg",
        "min",
        "max",
    ]
    assert schema.data["qso"]["fields"]["country"]["aggregate_functions"] == [
        "distinct_count"
    ]
    assert schema.data["qso"]["aggregation"]["group_by"]["all_fields"] is True
    assert schema.data["qso"]["aggregation"]["group_by"]["bucket_intervals"] == [
        "minute",
        "hour",
        "day",
        "week",
        "month",
        "quarter",
        "year",
    ]
    assert "including both boundaries" in schema.data["qso"][
        "filter_operator_semantics"
    ]["between"]
    assert "empty string does not match" in schema.data["qso"][
        "filter_operator_semantics"
    ]["is_null"]
    assert schema.data["qso"]["aggregation"]["group_by"]["interval_semantics"][
        "week"
    ].startswith("UTC week represented by its Monday")
    assert schema.data["qso"]["aggregation"]["processing_order"][:3] == [
        "scope and top-level filters",
        "requested list expansion",
        "one_per_group",
    ]
    assert "typed tuples" in schema.data["qso"]["aggregation"][
        "composite_distinct_count"
    ]
    assert "Greater than or equal" in schema.data["qso"]["aggregation"][
        "having_operator_semantics"
    ]["gte"]
    assert schema.data["qso"]["aggregation"]["group_by"]["derived"]["hour"] == {
        "type": "integer",
        "description": "UTC QSO hour of day from 0 to 23",
    }
    assert schema.data["qso"]["aggregation"]["functions"]["count"] == (
        "Count QSO rows; field must be omitted"
    )
    assert schema.data["qso"]["aggregation"]["having_operators"] == [
        "eq",
        "neq",
        "gt",
        "gte",
        "lt",
        "lte",
    ]
    assert schema.data["qso"]["band_frequency_filtering"]["bands"] == [
        {"name": "40m", "start_mhz": 7.0, "end_mhz": 7.3},
        {"name": "20m", "start_mhz": 14.0, "end_mhz": 14.35},
        {"name": "15m", "start_mhz": 21.0, "end_mhz": 21.45},
    ]
    assert "exact" in schema.data["qso"]["fields"]["frequency"]["description"]
    assert "frequency-only" in schema.data["qso"]["fields"]["band"]["description"]
    assert "filter_operators" not in schema.data["qso"]
    assert "profile_name" not in schema.data["qso"]["fields"]


async def test_qso_schema_describes_list_fields_and_station_side(qlog_database) -> None:
    connection = sqlite3.connect(qlog_database)
    for column in (
        "my_pota_ref",
        "vucc_grids",
        "my_vucc_grids",
        "usaca_counties",
        "my_usaca_counties",
        "cnty_alt",
        "my_cnty_alt",
        "credit_submitted",
        "credit_granted",
        "award_submitted",
        "award_granted",
        "contacted_op",
    ):
        connection.execute(f'ALTER TABLE contacts ADD COLUMN "{column}" TEXT')
    connection.commit()
    connection.close()

    async with Client(create_server(qlog_database)) as client:
        schema = await client.call_tool("qlog.get_schema", {"domain": "qso"})

    fields = schema.data["qso"]["fields"]
    list_fields = {
        "pota_ref": "pota_reference",
        "my_pota_ref": "pota_reference",
        "vucc_grids": "maidenhead_grid",
        "my_vucc_grids": "maidenhead_grid",
        "usaca_counties": "secondary_subdivision",
        "my_usaca_counties": "secondary_subdivision",
        "county_alt": "alternate_secondary_subdivision",
        "my_county_alt": "alternate_secondary_subdivision",
        "credit_submitted": "credit",
        "credit_granted": "credit",
        "award_submitted": "sponsored_award",
        "award_granted": "sponsored_award",
    }
    assert {
        name for name, field in fields.items() if field["cardinality"] == "many"
    } == set(list_fields)
    for name, item_type in list_fields.items():
        assert fields[name]["item_type"] == item_type

    for contacted, logging in (
        ("callsign", "station_callsign"),
        ("pota_ref", "my_pota_ref"),
        ("vucc_grids", "my_vucc_grids"),
        ("usaca_counties", "my_usaca_counties"),
        ("county_alt", "my_county_alt"),
    ):
        assert fields[contacted]["side"] == "contacted"
        assert fields[contacted]["paired_field"] == logging
        assert fields[logging]["side"] == "logging"
        assert fields[logging]["paired_field"] == contacted

    assert fields["operator"]["side"] == "operator"
    assert fields["operator"]["paired_field"] == "contacted_operator"
    assert fields["credit_granted"]["side"] == "qso"
    assert "colon-delimited" in fields["usaca_counties"]["description"]
    assert "semicolon-delimited" in fields["county_alt"]["description"]
    assert "@ location suffix" in fields["pota_ref"]["description"]
    assert "'&'" in fields["credit_granted"]["description"]


async def test_grid4_fields_return_only_valid_maidenhead_squares(qlog_database) -> None:
    connection = sqlite3.connect(qlog_database)
    connection.execute("ALTER TABLE contacts ADD COLUMN gridsquare TEXT")
    connection.executemany(
        "UPDATE contacts SET gridsquare = ?, my_gridsquare = ? WHERE id = ?",
        [
            ("JO70", "JO70AA", 1),
            ("jn89aa", "JN89AA12", 2),
            ("JN89AA12", "", 3),
            ("ZZ99", "JO7", 4),
        ],
    )
    connection.commit()
    connection.close()

    async with Client(create_server(qlog_database)) as client:
        schema = await client.call_tool("qlog.get_schema", {"domain": "qso"})
        result = await client.call_tool(
            "qso.query",
            {
                "scope": {"station_scope": "all"},
                "fields": ["id", "grid4", "my_grid4"],
                "sort": [{"field": "id", "direction": "asc"}],
            },
        )

    assert result.data["items"] == [
        {"id": 1, "grid4": "JO70", "my_grid4": "JO70"},
        {"id": 2, "grid4": "JN89", "my_grid4": "JN89"},
        {"id": 3, "grid4": "JN89", "my_grid4": None},
        {"id": 4, "grid4": None, "my_grid4": None},
    ]
    fields = schema.data["qso"]["fields"]
    assert fields["grid4"]["paired_field"] == "my_grid4"
    assert fields["my_grid4"]["paired_field"] == "grid4"
    assert fields["grid4"]["cardinality"] == "one"


async def test_qso_query_filters_sorts_and_pages(qlog_database) -> None:
    arguments = {
        "scope": {
            "station_scope": "all",
            "date_from": "2026-01-01",
            "date_to": "2026-12-31",
        },
        "filters": {
            "logic": "and",
            "conditions": [
                {"field": "dxcc", "op": "eq", "value": 339},
                {"field": "band", "op": "in", "value": ["15M", "20m"]},
            ],
            "groups": [
                {
                    "logic": "or",
                    "conditions": [
                        {"field": "mode", "op": "eq", "value": "cw"},
                        {"field": "mode", "op": "eq", "value": "FT8"},
                    ],
                }
            ],
        },
        "fields": ["id", "datetime", "callsign", "band", "mode", "adif_mode", "extra_fields"],
        "sort": [{"field": "datetime", "direction": "desc"}],
        "limit": 2,
    }

    async with Client(create_server(qlog_database)) as client:
        result = await client.call_tool("qso.query", arguments)

    assert [item["id"] for item in result.data["items"]] == [3, 2]
    assert result.data["items"][0]["datetime"] == "2026-02-20T08:00:00Z"
    assert result.data["items"][0]["extra_fields"] == "not-json"
    assert result.data["items"][1]["mode"] == "FT8"
    assert result.data["items"][1]["adif_mode"] == "MFSK"
    assert result.data["items"][1]["extra_fields"]["app_qlog_test"]["value"] == "new"
    assert result.data["page"] == {
        "limit": 2,
        "offset": 0,
        "returned": 2,
        "has_more": False,
        "next_offset": None,
    }
    assert result.data["effective_scope"] == {
        "station_scope": "all",
        "date_from": "2026-01-01",
        "date_to": "2026-12-31",
    }


async def test_qso_query_profile_scope_and_pagination(qlog_database) -> None:
    arguments = {
        "scope": {
            "station_scope": "profile",
            "station_profile_names": ["portable"],
        },
        "filters": {
            "conditions": [{"field": "callsign", "op": "starts_with", "value": "ja"}]
        },
        "fields": ["id", "callsign"],
        "sort": [{"field": "id", "direction": "asc"}],
        "limit": 2,
    }

    async with Client(create_server(qlog_database)) as client:
        first_page = await client.call_tool("qso.query", arguments)
        arguments["offset"] = first_page.data["page"]["next_offset"]
        second_page = await client.call_tool("qso.query", arguments)

    assert [item["id"] for item in first_page.data["items"]] == [1, 2]
    assert first_page.data["page"]["has_more"] is True
    assert [item["id"] for item in second_page.data["items"]] == [3]
    assert second_page.data["page"]["has_more"] is False


@pytest.mark.parametrize(
    ("keep", "expected_ids"),
    [("first", [2, 1]), ("last", [3, 2])],
)
async def test_qso_query_keeps_first_or_last_qso_per_group(
    qlog_database, keep, expected_ids
) -> None:
    async with Client(create_server(qlog_database)) as client:
        result = await client.call_tool(
            "qso.query",
            {
                "scope": {"station_scope": "all"},
                "filters": {
                    "conditions": [{"field": "continent", "value": "AS"}]
                },
                "fields": ["id", "datetime", "callsign"],
                "one_per_group": {
                    "fields": ["dxcc", "band"],
                    "keep": keep,
                },
                "sort": [{"field": "datetime", "direction": "desc"}],
            },
        )

    assert [item["id"] for item in result.data["items"]] == expected_ids


async def test_qso_query_reads_autovalue_fields_with_left_join(qlog_database) -> None:
    async with Client(create_server(qlog_database)) as client:
        result = await client.call_tool(
            "qso.query",
            {
                "scope": {"station_scope": "all"},
                "fields": [
                    "id",
                    "base_callsign",
                    "wavelog_upload_status",
                    "wavelog_upload_date",
                ],
                "sort": [{"field": "id", "direction": "asc"}],
            },
        )
        filtered = await client.call_tool(
            "qso.query",
            {
                "scope": {"station_scope": "all"},
                "filters": {
                    "conditions": [
                        {"field": "base_callsign", "op": "eq", "value": "ja1aaa"}
                    ]
                },
                "fields": ["id"],
            },
        )

    assert result.data["items"][0] == {
        "id": 1,
        "base_callsign": "JA1AAA",
        "wavelog_upload_status": "Y",
        "wavelog_upload_date": "2026-03-01",
    }
    assert result.data["items"][-1] == {
        "id": 4,
        "base_callsign": None,
        "wavelog_upload_status": None,
        "wavelog_upload_date": None,
    }
    assert filtered.data["items"] == [{"id": 1}]


async def test_band_filter_and_group_use_stored_band_or_frequency(qlog_database) -> None:
    connection = sqlite3.connect(qlog_database)
    connection.execute("UPDATE contacts SET band = '' WHERE id = 1")
    connection.execute("UPDATE contacts SET freq = NULL WHERE id = 2")
    connection.commit()
    connection.close()

    async with Client(create_server(qlog_database)) as client:
        band_20m = await client.call_tool(
            "qso.query",
            {
                "scope": {"station_scope": "all"},
                "filters": {"conditions": [{"field": "band", "value": "20m"}]},
                "fields": ["id", "band", "frequency"],
                "sort": [{"field": "id", "direction": "asc"}],
            },
        )
        band_15m = await client.call_tool(
            "qso.query",
            {
                "scope": {"station_scope": "all"},
                "filters": {"conditions": [{"field": "band", "value": "15m"}]},
                "fields": ["id", "band", "frequency"],
            },
        )
        exact_frequency = await client.call_tool(
            "qso.query",
            {
                "scope": {"station_scope": "all"},
                "filters": {"conditions": [{"field": "frequency", "value": 14.025}]},
                "fields": ["id"],
            },
        )
        missing_exact_frequency = await client.call_tool(
            "qso.query",
            {
                "scope": {"station_scope": "all"},
                "filters": {"conditions": [{"field": "frequency", "value": 21.074}]},
                "fields": ["id"],
            },
        )
        grouped = await client.call_tool(
            "qso.aggregate",
            {
                "scope": {"station_scope": "all"},
                "filters": {
                    "conditions": [{"field": "band", "op": "in", "value": ["20m", "15m"]}]
                },
                "group_by": ["band"],
                "metrics": [{"function": "count", "as": "qso_count"}],
            },
        )

    assert band_20m.data["items"] == [
        {"id": 1, "band": "20m", "frequency": 14.025},
        {"id": 3, "band": "20m", "frequency": 14.035},
        {"id": 4, "band": "20m", "frequency": 14.25},
    ]
    assert band_15m.data["items"] == [{"id": 2, "band": "15m", "frequency": None}]
    assert exact_frequency.data["items"] == [{"id": 1}]
    assert missing_exact_frequency.data["items"] == []
    assert grouped.data["rows"] == [
        {"band": "20m", "qso_count": 3},
        {"band": "15m", "qso_count": 1},
    ]


async def test_derived_technical_fields_query_and_schema(qlog_database) -> None:
    connection = sqlite3.connect(qlog_database)
    connection.execute("ALTER TABLE contacts ADD COLUMN freq_rx REAL")
    connection.execute("ALTER TABLE contacts ADD COLUMN band_rx TEXT")
    connection.executemany(
        "UPDATE contacts SET freq_rx = ?, band_rx = ? WHERE id = ?",
        [
            (14.027, "20m", 1),
            (432.1, "70cm", 2),
            (14.035, "20m", 3),
        ],
    )
    connection.execute("UPDATE contacts SET rst_sent = '5NN' WHERE id = 4")
    connection.commit()
    connection.close()

    fields = [
        "id",
        "is_split",
        "is_cross_band",
        "frequency_offset_khz",
        "duration_seconds",
        "rst_sent_numeric",
        "rst_received_numeric",
    ]
    async with Client(create_server(qlog_database)) as client:
        schema = await client.call_tool("qlog.get_schema")
        result = await client.call_tool(
            "qso.query",
            {
                "scope": {"station_scope": "all"},
                "fields": fields,
                "sort": [{"field": "id", "direction": "asc"}],
            },
        )
        split = await client.call_tool(
            "qso.query",
            {
                "scope": {"station_scope": "all"},
                "filters": {"conditions": [{"field": "is_split", "value": True}]},
                "fields": ["id"],
                "sort": [{"field": "id", "direction": "asc"}],
            },
        )
        grouped = await client.call_tool(
            "qso.aggregate",
            {
                "scope": {"station_scope": "all"},
                "group_by": ["is_split"],
                "metrics": [{"function": "count", "as": "qso_count"}],
                "order_by": [{"field": "is_split", "direction": "asc"}],
            },
        )

    rows = result.data["items"]
    assert [
        (
            row["id"],
            row["is_split"],
            row["is_cross_band"],
            row["frequency_offset_khz"],
            row["duration_seconds"],
            row["rst_sent_numeric"],
            row["rst_received_numeric"],
        )
        for row in rows
    ] == [
        (1, True, False, 2.0, 120, 599, 579),
        (2, True, True, 411026.0, 60, -10, -12),
        (3, False, False, 0.0, 300, 599, 599),
        (4, False, False, None, 180, None, 59),
    ]
    assert split.data["items"] == [{"id": 1}, {"id": 2}]
    assert grouped.data["rows"] == [
        {"is_split": False, "qso_count": 2},
        {"is_split": True, "qso_count": 2},
    ]
    qso_fields = schema.data["qso"]["fields"]
    assert qso_fields["is_split"]["type"] == "boolean"
    assert qso_fields["is_split"]["operators"] == ["eq", "neq", "in", "not_in"]
    assert "1 Hz" in qso_fields["is_split"]["description"]
    assert "different scales" in qso_fields["rst_sent_numeric"]["description"]


async def test_qso_query_rejects_unknown_semantic_field(qlog_database) -> None:
    async with Client(create_server(qlog_database)) as client:
        result = await client.call_tool(
            "qso.query",
            {"scope": {"station_scope": "all"}, "fields": ["raw_sql"]},
            raise_on_error=False,
        )

    assert result.is_error is True
    assert "Unknown QSO field" in result.content[0].text


async def test_qso_query_rejects_operator_not_published_for_field(qlog_database) -> None:
    async with Client(create_server(qlog_database)) as client:
        result = await client.call_tool(
            "qso.query",
            {
                "scope": {"station_scope": "all"},
                "filters": {
                    "conditions": [{"field": "dxcc", "op": "contains", "value": "33"}]
                },
            },
            raise_on_error=False,
        )

    assert result.is_error is True
    assert "not supported for QSO field 'dxcc'" in result.content[0].text


async def test_qso_query_requires_station_scope(qlog_database) -> None:
    async with Client(create_server(qlog_database)) as client:
        result = await client.call_tool("qso.query", raise_on_error=False)

    assert result.is_error is True
    assert "scope" in result.content[0].text


@pytest.mark.parametrize(
    "scope",
    [
        {"station_scope": "callsign"},
        {
            "station_scope": "all",
            "station_callsigns": [{"callsign": "OK1MLG"}],
        },
    ],
)
async def test_qso_query_rejects_inconsistent_station_scope(qlog_database, scope) -> None:
    async with Client(create_server(qlog_database)) as client:
        result = await client.call_tool(
            "qso.query", {"scope": scope}, raise_on_error=False
        )

    assert result.is_error is True
    assert "station_callsigns" in result.content[0].text


async def test_qso_query_callsign_and_grid_scope(qlog_database) -> None:
    async with Client(create_server(qlog_database)) as client:
        result = await client.call_tool(
            "qso.query",
            {
                "scope": {
                    "station_scope": "callsign",
                    "station_callsigns": [{"callsign": "ok1mlg", "grid": "jo80bb"}],
                },
                "fields": ["id"],
                "sort": [{"field": "id", "direction": "asc"}],
            },
        )

    assert [item["id"] for item in result.data["items"]] == [4]


async def test_qso_query_adapts_default_projection_to_available_columns(tmp_path) -> None:
    database = tmp_path / "minimal-qlog.sqlite"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE contacts (id INTEGER PRIMARY KEY, start_time TEXT, callsign TEXT)"
    )
    connection.execute(
        "INSERT INTO contacts VALUES (?, ?, ?)",
        (1, "2026-03-01T10:00:00Z", "OK1ABC"),
    )
    connection.commit()
    connection.close()

    async with Client(create_server(database)) as client:
        context = await client.call_tool("qlog.get_context")
        schema = await client.call_tool("qlog.get_schema")
        result = await client.call_tool(
            "qso.query", {"scope": {"station_scope": "all"}}
        )

    assert context.data["station_callsigns"] == []
    assert "base_callsign" not in schema.data["qso"]["fields"]
    assert schema.data["qso"]["fields"]["callsign"]["side"] == "contacted"
    assert "paired_field" not in schema.data["qso"]["fields"]["callsign"]
    assert "grid4" not in schema.data["qso"]["fields"]
    assert "my_grid4" not in schema.data["qso"]["fields"]
    assert result.data["fields"] == ["id", "datetime", "callsign"]
    assert result.data["items"] == [
        {"id": 1, "datetime": "2026-03-01T10:00:00Z", "callsign": "OK1ABC"}
    ]


@pytest.mark.parametrize(
    ("filters", "expected_ids"),
    [
        ({"conditions": [{"field": "callsign", "op": "eq", "value": "ja2bbb"}]}, [2]),
        ({"conditions": [{"field": "dxcc", "op": "neq", "value": 339}]}, [4]),
        ({"conditions": [{"field": "band", "op": "in", "value": ["15M"]}]}, [2]),
        ({"conditions": [{"field": "band", "op": "not_in", "value": ["20m"]}]}, [2]),
        ({"conditions": [{"field": "frequency", "op": "gt", "value": 21}]}, [2]),
        ({"conditions": [{"field": "frequency", "op": "gte", "value": 21.074}]}, [2]),
        ({"conditions": [{"field": "frequency", "op": "lt", "value": 14.03}]}, [1]),
        ({"conditions": [{"field": "frequency", "op": "lte", "value": 14.025}]}, [1]),
        (
            {
                "conditions": [
                    {
                        "field": "datetime",
                        "op": "between",
                        "value": ["2026-02-20", "2026-02-22"],
                    }
                ]
            },
            [3, 4],
        ),
        (
            {
                "conditions": [
                    {"field": "datetime", "op": "eq", "value": "2026-02-20T08:00:00Z"}
                ]
            },
            [3],
        ),
        ({"conditions": [{"field": "country", "op": "contains", "value": "states"}]}, [4]),
        ({"conditions": [{"field": "callsign", "op": "starts_with", "value": "ja"}]}, [1, 2, 3]),
        ({"conditions": [{"field": "callsign", "op": "ends_with", "value": "bbb"}]}, [2]),
        ({"conditions": [{"field": "pota_ref", "op": "is_null"}]}, [1]),
        ({"conditions": [{"field": "pota_ref", "op": "is_not_null"}]}, [2, 3, 4]),
        ({"conditions": [{"field": "pota_ref", "op": "is_empty"}]}, [1, 3]),
        ({"conditions": [{"field": "pota_ref", "op": "is_not_empty"}]}, [2, 4]),
        (
            {"conditions": [{"field": "dxcc", "op": "eq", "value": 339}], "negate": True},
            [4],
        ),
        (
            {"conditions": [{"field": "callsign", "op": "eq", "value": "' OR 1=1 --"}]},
            [],
        ),
    ],
)
async def test_qso_query_filter_operators(qlog_database, filters, expected_ids) -> None:
    async with Client(create_server(qlog_database)) as client:
        result = await client.call_tool(
            "qso.query",
            {
                "scope": {"station_scope": "all"},
                "filters": filters,
                "fields": ["id"],
                "sort": [{"field": "id", "direction": "asc"}],
            },
        )

    assert [item["id"] for item in result.data["items"]] == expected_ids


async def test_qso_query_matches_semantic_list_items(list_qlog_database) -> None:
    async with Client(create_server(list_qlog_database)) as client:
        schema = (await client.call_tool("qlog.get_schema")).data["qso"]
        requests = (
            ("pota_ref", "has", "K-4562", [1]),
            ("pota_ref", "has", "K-4562@US-NV", []),
            ("pota_ref", "has_all", ["k-0001", "K-4562 @ US-CA"], [1]),
            ("vucc_grids", "has", "jo70aa", [1]),
            ("usaca_counties", "has", "ma, Hampshire", [1]),
            ("credit_granted", "has", "dxcc:lotw", [1]),
            ("credit_granted", "has", "DXCC:LOTW&CARD", []),
            (
                "credit_granted",
                "has_all",
                ["DXCC:LOTW", "DXCC:CARD"],
                [1],
            ),
            ("credit_granted", "has_any", ["NOPE", "unknown:card"], [3]),
            ("award_granted", "has", "pota", [1]),
            ("county_alt", "has", "nz_regions: Wairoa", [1]),
        )
        results = []
        for field, operator, value, expected in requests:
            result = await client.call_tool(
                "qso.query",
                {
                    "scope": {"station_scope": "all"},
                    "filters": {
                        "conditions": [{"field": field, "op": operator, "value": value}]
                    },
                    "fields": ["id"],
                    "sort": [{"field": "id", "direction": "asc"}],
                },
            )
            results.append(([item["id"] for item in result.data["items"]], expected))

        malformed = await client.call_tool(
            "qso.query",
            {
                "scope": {"station_scope": "all"},
                "filters": {
                    "conditions": [
                        {"field": "pota_ref", "op": "has", "value": "K-1000;K-2000"}
                    ]
                },
                "fields": ["id", "pota_ref"],
            },
        )

    assert all(actual == expected for actual, expected in results)
    assert malformed.data["items"] == [{"id": 3, "pota_ref": "K-1000;K-2000"}]
    assert schema["fields"]["pota_ref"]["operators"][-3:] == [
        "has",
        "has_any",
        "has_all",
    ]
    assert "has" not in schema["fields"]["callsign"]["operators"]
    assert "contains" in schema["list_values"]["malformed_values"]
    assert "@location" in schema["fields"]["pota_ref"]["list_semantics"]["matching"]
    assert schema["fields"]["credit_granted"]["list_semantics"]["explosion"] == (
        "Returns the credit name without its optional media."
    )


@pytest.mark.parametrize(
    ("field", "operator", "value", "message"),
    [
        ("callsign", "has", "K1ABC", "not supported"),
        ("pota_ref", "has_all", [], "non-empty list"),
        ("pota_ref", "has", 123, "non-empty string"),
    ],
)
async def test_qso_query_rejects_invalid_list_filter(
    list_qlog_database, field, operator, value, message
) -> None:
    async with Client(create_server(list_qlog_database)) as client:
        result = await client.call_tool(
            "qso.query",
            {
                "scope": {"station_scope": "all"},
                "filters": {
                    "conditions": [{"field": field, "op": operator, "value": value}]
                },
            },
            raise_on_error=False,
        )

    assert result.is_error is True
    assert message in result.content[0].text
