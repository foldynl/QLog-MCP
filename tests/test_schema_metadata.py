"""Public schema metadata and preservation of the underlying QSO values."""

import sqlite3
from pathlib import Path

import pytest
from fastmcp import Client

from qlog_mcp.server import create_server


@pytest.fixture
def metadata_database(qlog_database: Path) -> Path:
    with sqlite3.connect(qlog_database) as connection:
        for column, value_type in [
            ("freq_rx", "REAL"),
            ("distance", "REAL"),
            ("tx_pwr", "REAL"),
            ("rx_pwr", "REAL"),
            ("qsl_sent", "TEXT"),
            ("lotw_qsl_sent", "TEXT"),
            ("eqsl_qsl_rcvd", "TEXT"),
            ("eqsl_qsl_sent", "TEXT"),
            ("dcl_qsl_rcvd", "TEXT"),
            ("dcl_qsl_sent", "TEXT"),
            ("qsl_rcvd_via", "TEXT"),
            ("qsl_sent_via", "TEXT"),
            ("eqsl_ag", "TEXT"),
            ("altitude", "REAL"),
            ("my_altitude", "REAL"),
        ]:
            connection.execute(f'ALTER TABLE contacts ADD COLUMN "{column}" {value_type}')
        connection.execute(
            "UPDATE contacts SET freq_rx = 14.027, distance = 123.4, tx_pwr = 100, rx_pwr = 25, "
            "qsl_sent = 'R', lotw_qsl_sent = 'Y', eqsl_qsl_rcvd = 'R', eqsl_qsl_sent = 'Q', "
            "dcl_qsl_rcvd = 'V', dcl_qsl_sent = 'I', qsl_rcvd_via = 'D', "
            "qsl_sent_via = 'D', eqsl_ag = 'U', altitude = 125.5, my_altitude = 321"
        )
    return qlog_database


async def test_qso_schema_advertises_units_without_converting_values(
    metadata_database: Path,
) -> None:
    units = {
        "frequency": "MHz",
        "frequency_rx": "MHz",
        "frequency_offset_khz": "kHz",
        "distance": "km",
        "tx_power": "W",
        "rx_power": "W",
        "duration_seconds": "s",
        "altitude": "m",
        "my_altitude": "m",
    }
    async with Client(create_server(metadata_database)) as client:
        schema = (await client.call_tool("qlog.get_schema", {"domain": "qso"})).data["qso"]
        query = await client.call_tool(
            "qso.query",
            {
                "scope": {"station_scope": "all"},
                "fields": list(units),
                "filters": {"conditions": [{"field": "id", "value": 1}]},
            },
        )
    for name, unit in units.items():
        assert schema["fields"][name]["unit"] == unit
    for name in ["id", "callsign", "rst_sent_numeric", "qsl_received", "wavelog_upload_status"]:
        assert "unit" not in schema["fields"][name]
    assert query.data["items"] == [
        {
            "frequency": 14.025,
            "frequency_rx": 14.027,
            "frequency_offset_khz": 2.0,
            "distance": 123.4,
            "tx_power": 100.0,
            "rx_power": 25.0,
            "duration_seconds": 120,
            "altitude": 125.5,
            "my_altitude": 321.0,
        }
    ]


async def test_qsl_enum_metadata_explains_direction_and_legacy_status(
    metadata_database: Path,
) -> None:
    async with Client(create_server(metadata_database)) as client:
        fields = (await client.call_tool("qlog.get_schema")).data["qso"]["fields"]
    for name in ["qsl_received", "lotw_received", "eqsl_received", "dcl_received"]:
        enum = fields[name]["enum"]
        assert enum["name"] == "ADIF QSL Rcvd"
        assert set(enum["values"]) == {"Y", "N", "R", "I", "V"}
        assert "received" in enum["values"]["Y"].lower()
        assert "logging station" in enum["values"]["R"].lower()
        assert "import-only" in enum["values"]["V"].lower()
    for name in ["qsl_sent", "lotw_sent", "eqsl_sent", "dcl_sent"]:
        enum = fields[name]["enum"]
        assert enum["name"] == "ADIF QSL Sent"
        assert set(enum["values"]) == {"Y", "N", "R", "Q", "I"}
        assert "sent" in enum["values"]["Y"].lower()
        assert "contacted station" in enum["values"]["R"].lower()
        assert "do not" in enum["values"]["N"].lower()
        assert "queued" in enum["values"]["Q"].lower()
    assert fields["wavelog_upload_status"]["enum"] == {
        "name": "ADIF QSO Upload Status",
        "values": {
            "Y": "Uploaded and accepted by the online service",
            "N": "Do not upload to the online service",
            "M": "Modified after a previous upload",
        },
    }


async def test_qsl_route_metadata_distinguishes_actual_and_intended_routes(
    metadata_database: Path,
) -> None:
    async with Client(create_server(metadata_database)) as client:
        fields = (await client.call_tool("qlog.get_schema")).data["qso"]["fields"]
    for name, status in [("qsl_received_via", "qsl_received"), ("qsl_sent_via", "qsl_sent")]:
        enum = fields[name]["enum"]
        assert enum["name"] == "ADIF QSL Via"
        assert enum["values"]["B"] == "Bureau"
        assert enum["values"]["D"] == "Direct"
        assert enum["values"]["E"] == "Electronic"
        assert "import-only" in enum["values"]["M"]
        description = fields[name]["description"]
        assert status in description
        assert "intended" in description
        assert "unavailable" in description
    assert "V" in fields["qsl_received_via"]["description"]


async def test_qsl_route_does_not_imply_qsl_was_sent(metadata_database: Path) -> None:
    with sqlite3.connect(metadata_database) as connection:
        connection.execute("UPDATE contacts SET qsl_sent = 'Y' WHERE id = 2")
    async with Client(create_server(metadata_database)) as client:
        for conditions, ids in [
            ([{"field": "qsl_sent_via", "value": "D"}], [1, 2, 3, 4]),
            ([{"field": "qsl_sent_via", "value": "D"}, {"field": "qsl_sent", "value": "Y"}], [2]),
        ]:
            result = await client.call_tool(
                "qso.query",
                {
                    "scope": {"station_scope": "all"},
                    "fields": ["id"],
                    "filters": {"conditions": conditions},
                    "sort": [{"field": "id"}],
                },
            )
            assert [item["id"] for item in result.data["items"]] == ids


async def test_eqsl_ag_metadata_and_confirmation_are_independent(metadata_database: Path) -> None:
    with sqlite3.connect(metadata_database) as connection:
        connection.executemany(
            "UPDATE contacts SET eqsl_ag = ?, eqsl_qsl_rcvd = ? WHERE id = ?",
            [
                ("Y", "N", 1),
                ("N", "Y", 2),
                ("U", "Y", 3),
                ("Y", "Y", 4),
            ],
        )
    async with Client(create_server(metadata_database)) as client:
        field = (await client.call_tool("qlog.get_schema")).data["qso"]["fields"][
            "eqsl_authenticity_guaranteed"
        ]
        assert field["enum"]["name"] == "ADIF EQSL_AG"
        assert set(field["enum"]["values"]) == {"Y", "N", "U"}
        assert "unknown" in field["enum"]["values"]["U"].lower()
        assert "eqsl_received" in field["description"]
        for conditions, ids in [
            ([{"field": "eqsl_authenticity_guaranteed", "value": "Y"}], [1, 4]),
            ([{"field": "eqsl_received", "value": "Y"}], [2, 3, 4]),
            (
                [
                    {"field": "eqsl_authenticity_guaranteed", "value": "Y"},
                    {"field": "eqsl_received", "value": "Y"},
                ],
                [4],
            ),
            ([{"field": "eqsl_authenticity_guaranteed", "value": "U"}], [3]),
        ]:
            result = await client.call_tool(
                "qso.query",
                {
                    "scope": {"station_scope": "all"},
                    "fields": ["id"],
                    "filters": {"conditions": conditions},
                    "sort": [{"field": "id"}],
                },
            )
            assert [item["id"] for item in result.data["items"]] == ids


async def test_route_metadata_remains_available_without_status_column(qlog_database: Path) -> None:
    with sqlite3.connect(qlog_database) as connection:
        connection.execute("ALTER TABLE contacts ADD COLUMN qsl_sent_via TEXT")
    async with Client(create_server(qlog_database)) as client:
        fields = (await client.call_tool("qlog.get_schema")).data["qso"]["fields"]
    assert "qsl_sent" not in fields
    assert fields["qsl_sent_via"]["enum"]["name"] == "ADIF QSL Via"
    assert "unavailable" in fields["qsl_sent_via"]["description"]


@pytest.mark.parametrize(
    "field,column,value",
    [
        ("qsl_received", "qsl_rcvd", "V"),
        ("qsl_received", "qsl_rcvd", "LEGACY"),
        ("qsl_sent", "qsl_sent", "V"),
        ("qsl_received", "qsl_rcvd", ""),
        ("qsl_received", "qsl_rcvd", None),
        ("qsl_received_via", "qsl_rcvd_via", "M"),
        ("qsl_sent_via", "qsl_sent_via", "OLD_ROUTE"),
        ("eqsl_authenticity_guaranteed", "eqsl_ag", "LEGACY"),
        ("eqsl_authenticity_guaranteed", "eqsl_ag", ""),
        ("eqsl_authenticity_guaranteed", "eqsl_ag", None),
    ],
)
async def test_enum_metadata_does_not_restrict_or_rewrite_stored_values(
    metadata_database: Path, field: str, column: str, value: str | None
) -> None:
    with sqlite3.connect(metadata_database) as connection:
        connection.execute(f'UPDATE contacts SET "{column}" = ? WHERE id = 1', [value])
    filters = {"conditions": [{"field": field, "value": value}]}
    async with Client(create_server(metadata_database)) as client:
        query = await client.call_tool(
            "qso.query",
            {
                "scope": {"station_scope": "all"},
                "fields": ["id", field],
                "filters": filters,
            },
        )
        aggregate = await client.call_tool(
            "qso.aggregate",
            {
                "scope": {"station_scope": "all"},
                "group_by": [],
                "filters": filters,
                "metrics": [{"function": "count", "as": "qsos"}],
            },
        )
    assert query.data["items"] == [{"id": 1, field: value}]
    assert aggregate.data["rows"] == [{"qsos": 1}]


async def test_metadata_preserves_optional_field_availability(qlog_database: Path) -> None:
    async with Client(create_server(qlog_database)) as client:
        fields = (await client.call_tool("qlog.get_schema")).data["qso"]["fields"]
        result = await client.call_tool(
            "qso.query",
            {
                "scope": {"station_scope": "all"},
                "fields": ["tx_power"],
            },
            raise_on_error=False,
        )
    assert fields["frequency"]["unit"] == "MHz"
    assert fields["qsl_received"]["enum"]["name"] == "ADIF QSL Rcvd"
    assert "tx_power" not in fields
    assert "frequency_rx" not in fields
    assert "qsl_sent" not in fields
    for name in [
        "qsl_received_via",
        "qsl_sent_via",
        "eqsl_authenticity_guaranteed",
        "altitude",
        "my_altitude",
    ]:
        assert name not in fields
    assert result.is_error
    assert "unavailable" in result.content[0].text


async def test_schema_tool_description_explains_metadata_scope() -> None:
    async with Client(create_server()) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    description = tools["qlog.get_schema"].output_schema["description"]
    assert "unit" in description
    assert "enum" in description
    assert "metadata" in description
    assert "do not convert" in description
