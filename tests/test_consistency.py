"""Cross-tool consistency for time bounds, station selection and membership identity."""

import sqlite3
from pathlib import Path

import pytest
from fastmcp import Client

from qlog_mcp.server import create_server


@pytest.mark.parametrize(
    "times,first,last",
    [
        (
            ["2026-02-21T09:00:00Z", "2026-02-21 23:00:00"],
            "2026-02-21T09:00:00Z",
            "2026-02-21T23:00:00Z",
        ),
        (
            ["2026-02-21T00:30:00+02:00", "2026-02-20T23:30:00Z", "not-a-date", None],
            "2026-02-20T22:30:00Z",
            "2026-02-20T23:30:00Z",
        ),
        (["not-a-date", None], None, None),
        ([], None, None),
    ],
)
async def test_context_time_bounds_agree_with_query_and_aggregate(
    qlog_database: Path, times: list[str | None], first: str | None, last: str | None
) -> None:
    with sqlite3.connect(qlog_database) as connection:
        connection.execute("DELETE FROM contacts")
        connection.executemany(
            "INSERT INTO contacts (id, start_time, callsign) VALUES (?, ?, ?)",
            [(index, value, "K1ABC") for index, value in enumerate(times, 1)],
        )
    async with Client(create_server(qlog_database)) as client:
        context = (await client.call_tool("qlog.get_context")).data
        aggregate = (
            await client.call_tool(
                "qso.aggregate",
                {
                    "scope": {"station_scope": "all"},
                    "group_by": [],
                    "metrics": [
                        {"function": "min", "field": "datetime", "as": "first"},
                        {"function": "max", "field": "datetime", "as": "last"},
                    ],
                },
            )
        ).data
        for direction, expected in [("asc", first), ("desc", last)]:
            query = (
                await client.call_tool(
                    "qso.query",
                    {
                        "scope": {"station_scope": "all"},
                        "fields": ["datetime"],
                        "filters": {"conditions": [{"field": "datetime", "op": "is_not_null"}]},
                        "sort": [{"field": "datetime", "direction": direction}],
                        "limit": 1,
                    },
                )
            ).data
            assert query["items"] == ([] if expected is None else [{"datetime": expected}])
    assert context["qso_count"] == len(times)
    assert context["first_qso"] == aggregate["rows"][0]["first"] == first
    assert context["last_qso"] == aggregate["rows"][0]["last"] == last


@pytest.mark.parametrize(
    "contact_padding,profile_padding,request_padding",
    [
        (False, False, False),
        (True, False, False),
        (False, True, False),
        (False, False, True),
        (True, True, True),
    ],
)
async def test_offered_station_identifiers_select_the_same_qsos(
    qlog_database: Path, contact_padding: bool, profile_padding: bool, request_padding: bool
) -> None:
    with sqlite3.connect(qlog_database) as connection:
        connection.execute("UPDATE contacts SET operator = 'OP0ABC'")
        connection.execute("ALTER TABLE station_profiles ADD COLUMN operator_callsign TEXT")
        connection.execute("UPDATE station_profiles SET operator_callsign = 'OP0ABC'")
        if contact_padding:
            connection.execute(
                "UPDATE contacts SET station_callsign = ' ok1mlg ', "
                "my_gridsquare = ' ' || lower(my_gridsquare) || ' ', operator = ' op0abc '"
            )
        if profile_padding:
            connection.execute(
                "UPDATE station_profiles SET callsign = ' ok1mlg ', "
                "locator = ' ' || lower(locator) || ' ', operator_callsign = ' op0abc '"
            )
    async with Client(create_server(qlog_database)) as client:
        context = (await client.call_tool("qlog.get_context")).data
        callsign, grid, operator = "ok1mlg", "jo70aa", "op0abc"
        if request_padding:
            callsign, grid, operator = " ok1mlg ", " jo70aa ", " op0abc "
        selections = [
            (
                {"station_scope": "callsign", "station_callsigns": context["station_callsigns"]},
                [1, 2, 3, 4],
            ),
            (
                {"station_scope": "callsign", "station_callsigns": [{"callsign": callsign}]},
                [1, 2, 3, 4],
            ),
            (
                {
                    "station_scope": "callsign",
                    "station_callsigns": [{"callsign": callsign, "grid": grid}],
                },
                [1, 2, 3],
            ),
            (
                {
                    "station_scope": "callsign",
                    "station_callsigns": [{"callsign": callsign, "grid": "JO70"}],
                },
                [],
            ),
            ({"station_scope": "all", "operator_callsigns": context["operators"]}, [1, 2, 3, 4]),
            ({"station_scope": "all", "operator_callsigns": [operator]}, [1, 2, 3, 4]),
            ({"station_scope": "profile", "station_profile_names": ["Portable"]}, [1, 2, 3]),
            ({"station_scope": "profile", "station_profile_names": ["Home"]}, [4]),
        ]
        for scope, ids in selections:
            query = (
                await client.call_tool(
                    "qso.query",
                    {
                        "scope": scope,
                        "fields": ["id"],
                        "sort": [{"field": "id"}],
                    },
                )
            ).data
            aggregate = (
                await client.call_tool(
                    "qso.aggregate",
                    {
                        "scope": scope,
                        "group_by": [],
                        "metrics": [{"function": "count", "as": "qsos"}],
                    },
                )
            ).data
            assert [item["id"] for item in query["items"]] == ids
            assert aggregate["rows"] == [{"qsos": len(ids)}]


async def test_station_profile_keeps_free_text_matching_unchanged(qlog_database: Path) -> None:
    with sqlite3.connect(qlog_database) as connection:
        connection.execute("ALTER TABLE station_profiles ADD COLUMN qth_name TEXT")
        connection.execute("ALTER TABLE contacts ADD COLUMN my_city_intl TEXT")
        connection.execute("UPDATE station_profiles SET qth_name = 'City'")
        connection.execute("UPDATE contacts SET my_city_intl = ' City '")
    async with Client(create_server(qlog_database)) as client:
        result = await client.call_tool(
            "qso.query",
            {
                "scope": {"station_scope": "profile", "station_profile_names": ["Portable"]},
            },
        )
    assert result.data["items"] == []


@pytest.mark.parametrize("basis", ["qso_date", "directory_snapshot"])
@pytest.mark.parametrize(
    "callsigns,club_id,club_request,directory_id",
    [
        (["ja2bbb"], "OPEN", "OPEN", "OPEN"),
        (["JA2BBB"], "open", "OPEN", "OPEN"),
        (["JA2BBB"], " open ", "OPEN", "OPEN"),
        (["JA2BBB", "ja2bbb"], "OPEN", "OPEN", "OPEN"),
        (["JA2BBB", "ja2bbb"], "open", " open ", " OPEN "),
    ],
)
async def test_membership_fields_and_matching_share_case_insensitive_identity(
    catalog_qlog_database: Path,
    basis: str,
    callsigns: list[str],
    club_id: str,
    club_request: str,
    directory_id: str,
) -> None:
    with sqlite3.connect(catalog_qlog_database) as connection:
        connection.execute("DELETE FROM membership WHERE clubid = 'OPEN'")
        connection.execute(
            "UPDATE membership_directory SET short_desc = ? WHERE short_desc = 'OPEN'",
            [directory_id],
        )
        connection.executemany(
            "INSERT INTO membership VALUES (?, ?, ?, ?, ?)",
            [
                (callsign, str(index), "", "", club_id)
                for index, callsign in enumerate([*callsigns, "ZZ9NONE", "zz9none"])
            ],
        )
    field = "member_clubs_at_qso_date" if basis == "qso_date" else "member_clubs_in_directory"
    async with Client(create_server(catalog_qlog_database)) as client:
        query = await client.call_tool(
            "qso.query",
            {
                "scope": {"station_scope": "all"},
                "fields": ["id"],
                "filters": {"conditions": [{"field": field, "op": "has", "value": "OPEN"}]},
            },
        )
        results = []
        for relation in ["worked", "not_worked"]:
            results.append(
                (
                    await client.call_tool(
                        "membership.match_qso",
                        {
                            "scope": {"station_scope": "all"},
                            "club": club_request,
                            "membership_basis": basis,
                            "relation": relation,
                        },
                    )
                ).data
            )
    worked, not_worked = results
    assert query.data["items"] == [{"id": 2}]
    assert (
        worked["summary"]
        == not_worked["summary"]
        == {
            "member_callsigns": 2,
            "worked_callsigns": 1,
            "not_worked_callsigns": 1,
            "matching_qsos": 1,
            "invalid_membership_records": 0,
            "excluded_invalid_membership_records": 0,
        }
    )
    assert len(worked["items"]) == len(not_worked["items"]) == 1
    assert worked["items"][0]["callsign"].upper() == "JA2BBB"
    assert worked["items"][0]["qso_count"] == 1
    assert not_worked["items"][0]["callsign"].upper() == "ZZ9NONE"
    assert not_worked["items"][0]["qso_count"] == 0


@pytest.mark.parametrize("basis,excluded", [("qso_date", 2), ("directory_snapshot", 0)])
async def test_membership_invalid_date_counts_use_the_same_club_identity(
    catalog_qlog_database: Path, basis: str, excluded: int
) -> None:
    with sqlite3.connect(catalog_qlog_database) as connection:
        connection.execute("UPDATE membership SET clubid = ' broken ' WHERE clubid = 'BROKEN'")
    async with Client(create_server(catalog_qlog_database)) as client:
        result = await client.call_tool(
            "membership.match_qso",
            {
                "scope": {"station_scope": "all"},
                "club": "BROKEN",
                "membership_basis": basis,
            },
        )
    assert result.data["summary"]["invalid_membership_records"] == 2
    assert result.data["summary"]["excluded_invalid_membership_records"] == excluded


@pytest.mark.parametrize("basis", ["qso_date", "directory_snapshot"])
async def test_membership_case_variants_keep_distinct_qsos(
    catalog_qlog_database: Path, basis: str
) -> None:
    with sqlite3.connect(catalog_qlog_database) as connection:
        connection.execute(
            "UPDATE contacts_autovalue SET base_callsign = 'ja2bbb' WHERE contactid = 3"
        )
        connection.execute("INSERT INTO membership VALUES ('ja2bbb', '999', '', '', 'OPEN')")
    async with Client(create_server(catalog_qlog_database)) as client:
        result = await client.call_tool(
            "membership.match_qso",
            {
                "scope": {"station_scope": "all"},
                "club": "OPEN",
                "membership_basis": basis,
            },
        )
    assert result.data["summary"]["member_callsigns"] == 1
    assert result.data["summary"]["matching_qsos"] == 2
    assert len(result.data["items"]) == 1
    assert result.data["items"][0]["qso_count"] == 2
    assert result.data["items"][0]["first_qso"] == "2026-01-15T12:30:00Z"
    assert result.data["items"][0]["last_qso"] == "2026-02-20T08:00:00Z"


async def test_tool_contract_explains_selection_and_identity_rules() -> None:
    async with Client(create_server()) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    selector = tools["qso.query"].input_schema["$defs"]["StationCallsignSelector"]
    assert "surrounding spaces" in selector["description"]
    scope = tools["qso.query"].input_schema["$defs"]["LogScope"]
    assert "surrounding spaces" in scope["properties"]["operator_callsigns"]["description"]
    assert "case-insensitive" in tools["membership.match_qso"].description
    assert "invalid" in tools["qlog.get_context"].output_schema["description"]
