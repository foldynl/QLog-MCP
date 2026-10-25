"""Lossless shared schema definitions through the public MCP interface."""

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from qlog_mcp.qso import QSO_FIELDS
from qlog_mcp.server import create_server


@pytest.fixture
def all_fields_database(qlog_database: Path) -> Path:
    """Expose every registered capability without adding QSO data."""
    required: dict[str, set[str]] = {}
    for field in QSO_FIELDS.values():
        for column in field.required_columns or (field.column,):
            table, name = column.split(".", 1) if "." in column else ("contacts", column)
            required.setdefault(table, set()).add(name)
    with sqlite3.connect(qlog_database) as connection:
        for table, names in required.items():
            present = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
            if not present:
                connection.execute(f'CREATE TABLE "{table}" ("_fixture_id" INTEGER)')
            for name in sorted(names - present):
                connection.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" TEXT')
    return qlog_database


def expand_schema(compact: dict[str, Any]) -> dict[str, Any]:
    """Act as a client using only the documented reference and override rules."""
    expanded = {
        key: value
        for key, value in compact.items()
        if key not in {"field_types", "enums", "list_formats", "reference_rules", "fields"}
    }
    expanded["fields"] = {}
    for name, definition in compact["fields"].items():
        attributes = dict(definition)
        field = {**compact["field_types"][attributes.pop("type_ref")], **attributes}
        if "enum_ref" in field:
            field["enum"] = compact["enums"][field.pop("enum_ref")]
        if "list_format_ref" in field:
            field["list_semantics"] = compact["list_formats"][field.pop("list_format_ref")]
        expanded["fields"][name] = field
    return expanded


@pytest.mark.parametrize("database_fixture", ["qlog_database", "all_fields_database"])
async def test_compact_schema_preserves_every_definition_and_full_default(
    request: pytest.FixtureRequest, database_fixture: str
) -> None:
    database = request.getfixturevalue(database_fixture)
    async with Client(create_server(database)) as client:
        original = (await client.call_tool("qlog.get_schema")).data["qso"]
        compact = (await client.call_tool("qlog.get_schema", {"format": "compact"})).data["qso"]
        full = (await client.call_tool("qlog.get_schema", {"format": "full"})).data["qso"]
    assert original == full
    assert set(original) == {
        "fields",
        "default_fields",
        "filter_logic",
        "filter_group_negation",
        "filter_operator_semantics",
        "band_frequency_filtering",
        "list_values",
        "set_comparison",
        "aggregation",
    }
    assert expand_schema(compact) == original
    assert list(compact["fields"]) == list(original["fields"])
    assert compact["fields"]["frequency"]["unit"] == "MHz"
    assert compact["fields"]["callsign"]["paired_field"] == "station_callsign"
    assert compact["fields"]["qsl_received"]["enum_ref"] == "ADIF QSL Rcvd"
    assert "import-only" in compact["enums"]["ADIF QSL Rcvd"]["values"]["V"]
    assert compact["fields"]["pota_ref"]["list_format_ref"] == "pota_reference"
    assert compact["fields"]["pota_ref"]["type_ref"] == "string_list"
    assert "has" in compact["field_types"]["string_list"]["operators"]
    assert "has" not in compact["field_types"]["string"]["operators"]
    if database_fixture == "all_fields_database":
        assert set(compact["fields"]) == set(QSO_FIELDS)
        assert set(compact["field_types"]) == {
            "string",
            "string_list",
            "integer",
            "number",
            "boolean",
            "date",
            "datetime",
            "object",
        }
        assert compact["fields"]["lotw_received"]["enum_ref"] == "ADIF QSL Rcvd"
        assert compact["fields"]["qsl_sent"]["enum_ref"] == "ADIF QSL Sent"
        assert compact["fields"]["my_pota_ref"]["list_format_ref"] == "pota_reference"
        assert len(json.dumps(compact)) < len(json.dumps(full)) * 0.6
    else:
        assert "tx_power" not in compact["fields"]
        assert "my_pota_ref" not in compact["fields"]


async def test_shared_defaults_preserve_field_exceptions(
    qlog_database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(QSO_FIELDS, "callsign", replace(QSO_FIELDS["callsign"], sortable=False))
    monkeypatch.setitem(QSO_FIELDS, "country", replace(QSO_FIELDS["country"], filterable=False))
    monkeypatch.setitem(
        QSO_FIELDS,
        "lotw_received",
        replace(QSO_FIELDS["lotw_received"], enum_values={"Y": "A distinct service meaning"}),
    )
    async with Client(create_server(qlog_database)) as client:
        full = (await client.call_tool("qlog.get_schema")).data["qso"]
        compact = (await client.call_tool("qlog.get_schema", {"format": "compact"})).data["qso"]
    expanded = expand_schema(compact)
    assert expanded == full
    assert expanded["fields"]["callsign"]["sortable"] is False
    assert expanded["fields"]["country"]["operators"] == []
    assert expanded["fields"]["lotw_received"]["enum"]["values"] == {
        "Y": "A distinct service meaning"
    }


@pytest.mark.parametrize("format", ["full", "compact"])
@pytest.mark.parametrize(
    "operations,omitted",
    [
        (["query"], {"aggregation", "set_comparison"}),
        (["aggregate"], {"set_comparison"}),
        (["compare_sets"], {"aggregation"}),
        (["query", "aggregate", "compare_sets"], set()),
        (["query", "query"], {"aggregation", "set_comparison"}),
    ],
)
async def test_operation_selection_changes_only_requested_documentation(
    qlog_database: Path, format: str, operations: list[str], omitted: set[str]
) -> None:
    async with Client(create_server(qlog_database)) as client:
        full = (await client.call_tool("qlog.get_schema")).data["qso"]
        result = (
            await client.call_tool("qlog.get_schema", {"format": format, "operations": operations})
        ).data["qso"]
    expanded = expand_schema(result) if format == "compact" else result
    assert expanded == {key: value for key, value in full.items() if key not in omitted}


async def test_compact_metadata_can_build_a_query_and_does_not_disable_other_tools(
    qlog_database: Path,
) -> None:
    async with Client(create_server(qlog_database)) as client:
        schema = (
            await client.call_tool(
                "qlog.get_schema", {"format": "compact", "operations": ["query"]}
            )
        ).data["qso"]
        field = schema["fields"]["mode"]
        field_type = schema["field_types"][field["type_ref"]]
        assert "eq" in field_type["operators"]
        assert field_type["type"] == "string"
        assert schema["field_types"][schema["fields"]["datetime"]["type_ref"]]["sortable"]
        result = await client.call_tool(
            "qso.query",
            {
                "scope": {"station_scope": "all"},
                "fields": ["datetime", "callsign", "band", "mode"],
                "filters": {"conditions": [{"field": "mode", "op": "eq", "value": "CW"}]},
                "sort": [{"field": "datetime", "direction": "desc"}],
                "limit": 5,
            },
        )
        aggregate = await client.call_tool(
            "qso.aggregate",
            {
                "scope": {"station_scope": "all"},
                "group_by": [],
                "metrics": [{"function": "count", "as": "qsos"}],
            },
        )
    assert [item["callsign"] for item in result.data["items"]] == ["JA3CCC", "JA1AAA"]
    assert aggregate.data["rows"] == [{"qsos": 4}]


@pytest.mark.parametrize(
    "arguments,error",
    [
        ({"format": "compressed"}, "format"),
        ({"operations": []}, "operations"),
        ({"operations": ["unknown"]}, "operations"),
        ({"operations": "query"}, "operations"),
        ({"domain": "catalog", "format": "compact"}, "format"),
        ({"domain": "runtime", "format": "compact"}, "format"),
        ({"domain": "catalog", "operations": ["query"]}, "operations"),
        ({"domain": "runtime", "operations": ["query"]}, "operations"),
        ({"format": "compact", "sources": ["rig"]}, "sources"),
    ],
)
async def test_invalid_schema_options_fail_before_accessing_data(
    tmp_path: Path, arguments: dict[str, Any], error: str
) -> None:
    database = tmp_path / "missing.sqlite"
    async with Client(create_server(database)) as client:
        result = await client.call_tool("qlog.get_schema", arguments, raise_on_error=False)
    assert result.is_error
    assert error in result.content[0].text
    assert "Database" not in result.content[0].text
    assert "not running" not in result.content[0].text
    assert not database.exists()


async def test_compact_schema_contract_is_explained_before_database_access() -> None:
    server = create_server()
    async with Client(server) as client:
        tool = next(tool for tool in await client.list_tools() if tool.name == "qlog.get_schema")
    properties = tool.input_schema["properties"]
    assert properties["format"]["default"] == "full"
    assert properties["format"]["enum"] == ["full", "compact"]
    operation_list = properties["operations"]["anyOf"][0]
    assert operation_list["minItems"] == 1
    assert set(operation_list["items"]["enum"]) == {"query", "aggregate", "compare_sets"}
    assert "field_types" in tool.output_schema["description"]
    assert "type_ref" in tool.output_schema["description"]
    assert "override" in tool.output_schema["description"]
    assert "enum_ref" in tool.output_schema["description"]
    assert "list_format_ref" in tool.output_schema["description"]
    assert "format='compact'" in server.instructions
