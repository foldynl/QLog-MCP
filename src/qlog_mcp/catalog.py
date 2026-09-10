"""Read-only semantic queries over QLog reference catalogs."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Any, Literal

import aiosqlite
from pydantic import BaseModel, Field

from .database import Database
from .errors import IncompatibleDatabaseError, InvalidQueryError
from .filters import (
    BOOLEAN_OPERATORS,
    COMPARISON_OPERATORS,
    FILTER_OPERATOR_DESCRIPTIONS,
    PRESENCE_OPERATORS,
    TEXT_OPERATORS,
    FilterCondition,
    FilterGroup,
    FilterOperator,
    SortDirection,
    compact_date_expression,
)
from .qso import LogScope, QsoQuery
from .usage_log import record_sql

CatalogName = Literal[
    "pota",
    "sota",
    "wwff",
    "iota",
    "dxcc",
    "satellite",
    "membership",
    "membership_clubs",
]
CatalogValueType = Literal["string", "integer", "number", "boolean", "date"]
CATALOG_NAMES: tuple[CatalogName, ...] = (
    "pota",
    "sota",
    "wwff",
    "iota",
    "dxcc",
    "satellite",
    "membership",
    "membership_clubs",
)
MAX_LIMIT = 1000


class CatalogSort(BaseModel):
    """One semantic catalog field used to order directory entries."""

    field: str = Field(
        description="Sortable semantic field advertised for the selected catalog."
    )
    direction: SortDirection = Field(
        default=SortDirection.ASC,
        description="Sort direction for this catalog field.",
    )


class MatchRelation(str, Enum):
    """Neutral set relationship between filtered catalog and QSO keys."""

    MATCHED = "matched"
    NOT_MATCHED = "not_matched"
    QSO_ONLY = "qso_only"


MATCH_RELATION_DESCRIPTIONS = {
    MatchRelation.MATCHED: (
        "Catalog keys occurring in at least one QSO after station scope and QSO filters."
    ),
    MatchRelation.NOT_MATCHED: (
        "Catalog keys absent from the QSOs remaining after station scope and QSO filters."
    ),
    MatchRelation.QSO_ONLY: (
        "Non-empty QSO keys absent from the catalog after its filters; each result includes "
        "the number of QSOs containing that key."
    ),
}


class CatalogMatchSort(BaseModel):
    """One result field used to order a catalog/QSO set comparison."""

    field: str = Field(
        description=(
            "Catalog field for matched or not_matched results. For qso_only use key or "
            "qso_count. A catalog sort field need not also be returned in fields."
        )
    )
    direction: SortDirection = Field(
        default=SortDirection.ASC,
        description="Sort direction for this comparison result field.",
    )


class MembershipMatchRelation(str, Enum):
    """Relationship between downloaded club members and scoped QSOs."""

    WORKED = "worked"
    NOT_WORKED = "not_worked"


class MembershipMatchBasis(str, Enum):
    """How membership rows are related to scoped QSOs."""

    QSO_DATE = "qso_date"
    DIRECTORY_SNAPSHOT = "directory_snapshot"


class MembershipMatchSort(BaseModel):
    """One returned membership/QSO result field used to order detail rows."""

    field: Literal["callsign", "qso_count", "first_qso", "last_qso"] = Field(
        description="Returned membership-match field used for ordering."
    )
    direction: SortDirection = Field(
        default=SortDirection.ASC,
        description="Sort direction for this result field.",
    )


@dataclass(frozen=True)
class CatalogField:
    column: str
    value_type: CatalogValueType
    description: str
    sql_expression: str | None = None

    def expression(self, table_alias: str = "d") -> str:
        expression = self.sql_expression or f'{{table_alias}}."{self.column}"'
        return expression.format(table_alias=table_alias)

    def comparison_expression(self, table_alias: str = "d") -> str:
        expression = self.expression(table_alias)
        if self.value_type == "string":
            return f"{expression} COLLATE NOCASE"
        return expression

    def operators(self) -> tuple[FilterOperator, ...]:
        comparison = BOOLEAN_OPERATORS if self.value_type == "boolean" else COMPARISON_OPERATORS
        text = TEXT_OPERATORS if self.value_type == "string" else ()
        return comparison + text + PRESENCE_OPERATORS


@dataclass(frozen=True)
class CatalogSource:
    table: str
    fields: dict[str, CatalogField]
    required_columns: frozenset[str]
    capability: Literal["full", "reduced"] = "full"


@dataclass(frozen=True)
class CatalogDefinition:
    description: str
    sources: tuple[CatalogSource, ...]
    default_fields: tuple[str, ...]
    key_field: str
    qso_fields: tuple[str, ...]
    tie_breaker_fields: tuple[str, ...] = ()

    @property
    def field_names(self) -> set[str]:
        return {name for source in self.sources for name in source.fields}


@dataclass(frozen=True)
class ResolvedCatalog:
    name: CatalogName
    definition: CatalogDefinition
    source: CatalogSource
    fields: dict[str, CatalogField]
    capability: Literal["full", "reduced"]


def _field(column: str, value_type: CatalogValueType, description: str) -> CatalogField:
    return CatalogField(column, value_type, description)


def _date_field(column: str, description: str) -> CatalogField:
    value = f'TRIM(CAST({{table_alias}}."{column}" AS TEXT))'
    iso_date = f"SUBSTR({value}, 1, 10)"
    day_first_date = (
        f"SUBSTR({value}, 7, 4) || '-' || SUBSTR({value}, 4, 2) || '-' || "
        f"SUBSTR({value}, 1, 2)"
    )
    normalized = (
        f"CASE WHEN SUBSTR({value}, 3, 1) = '/' AND SUBSTR({value}, 6, 1) = '/' "
        f"THEN {day_first_date} ELSE {iso_date} END"
    )
    expression = (
        f"CASE WHEN {value} = '' OR {value} = '0000-00-00' THEN NULL "
        f"WHEN DATE({normalized}, '+0 days') = {normalized} THEN {normalized} END"
    )
    return CatalogField(column, "date", description, expression)


def _compact_date_field(column: str, description: str) -> CatalogField:
    value = f'TRIM(CAST({{table_alias}}."{column}" AS TEXT))'
    return CatalogField(column, "date", description, compact_date_expression(value))


def _compact_date_state_field(column: str, description: str) -> CatalogField:
    value = f'TRIM(CAST({{table_alias}}."{column}" AS TEXT))'
    normalized = compact_date_expression(value)
    expression = (
        f"CASE WHEN NULLIF({value}, '') IS NULL THEN 'open' "
        f"WHEN ({normalized}) IS NULL THEN 'invalid' ELSE 'valid' END"
    )
    return CatalogField(column, "string", description, expression)


POTA_FIELDS = {
    "reference": _field(
        "reference", "string", "POTA park reference as stored in QLog's POTA directory"
    ),
    "name": _field("name", "string", "Park name"),
    "active": _field(
        "active", "boolean", "Directory active flag; this is catalog data, not a QSO result"
    ),
    "dxcc": _field("entityID", "integer", "Numeric DXCC entity code assigned to the park"),
    "location": _field(
        "locationDesc", "string", "POTA location descriptor supplied by the directory"
    ),
    "latitude": _field("latitude", "number", "Park latitude in decimal degrees"),
    "longitude": _field("longitude", "number", "Park longitude in decimal degrees"),
    "grid": _field("grid", "string", "Park Maidenhead locator supplied by the directory"),
}

SOTA_FIELDS = {
    "reference": _field("summit_code", "string", "SOTA summit reference"),
    "association": _field("association_name", "string", "SOTA association name"),
    "region": _field("region_name", "string", "SOTA region name"),
    "name": _field("summit_name", "string", "Summit name"),
    "altitude_m": _field("altm", "integer", "Summit altitude in metres"),
    "altitude_ft": _field("altft", "integer", "Summit altitude in feet"),
    "longitude": _field("longitude", "number", "Summit longitude in decimal degrees"),
    "latitude": _field("latitude", "number", "Summit latitude in decimal degrees"),
    "points": _field("points", "integer", "Points value stored in the SOTA directory"),
    "bonus_points": _field(
        "bonus_points", "integer", "Bonus points value stored in the SOTA directory"
    ),
    "valid_from": _date_field(
        "valid_from", "First validity date from the directory, normalized to YYYY-MM-DD"
    ),
    "valid_to": _date_field(
        "valid_to", "Last validity date from the directory, normalized to YYYY-MM-DD"
    ),
}

WWFF_FIELDS = {
    "reference": _field("reference", "string", "WWFF reference"),
    "status": _field("status", "string", "Status value supplied by the WWFF directory"),
    "name": _field("name", "string", "Protected-area name"),
    "program": _field("program", "string", "WWFF national or regional program identifier"),
    "dxcc": _field("dxcc", "string", "DXCC value supplied by the WWFF directory"),
    "state": _field("state", "string", "Primary subdivision supplied by the directory"),
    "county": _field("county", "string", "Secondary subdivision supplied by the directory"),
    "continent": _field("continent", "string", "Continent code supplied by the directory"),
    "iota": _field("iota", "string", "IOTA reference associated with the protected area"),
    "grid": _field("iaruLocator", "string", "IARU Maidenhead locator"),
    "latitude": _field("latitude", "number", "Protected-area latitude in decimal degrees"),
    "longitude": _field(
        "longitude", "number", "Protected-area longitude in decimal degrees"
    ),
    "iucn_category": _field(
        "iucncat", "string", "IUCN protected-area category supplied by the directory"
    ),
    "valid_from": _date_field(
        "valid_from", "First validity date from the directory, normalized to YYYY-MM-DD"
    ),
    "valid_to": _date_field(
        "valid_to", "Last validity date from the directory, normalized to YYYY-MM-DD"
    ),
}

IOTA_FIELDS = {
    "reference": _field("iotaid", "string", "IOTA island-group reference in CC-NNN form"),
    "name": _field("islandname", "string", "IOTA island-group name"),
}

DXCC_CLUBLOG_FIELDS = {
    "code": _field("id", "integer", "ADIF numeric DXCC entity code"),
    "name": _field("name", "string", "DXCC entity name"),
    "prefix": _field("prefix", "string", "Representative entity prefix"),
    "deleted": _field(
        "deleted", "boolean", "Whether the directory marks this as a deleted DXCC entity"
    ),
    "continent": _field("cont", "string", "Continent code"),
    "cq_zone": _field("cqz", "integer", "Default CQ zone supplied by the directory"),
    "itu_zone": _field("ituz", "integer", "Default ITU zone supplied by the directory"),
    "latitude": _field("lat", "number", "Representative entity latitude in decimal degrees"),
    "longitude": _field(
        "lon", "number", "Representative entity longitude in decimal degrees"
    ),
    "valid_from": _date_field(
        "start", "Entity start date from the directory, normalized to YYYY-MM-DD"
    ),
    "valid_to": _date_field(
        "end", "Entity end date from the directory, normalized to YYYY-MM-DD"
    ),
}

DXCC_AD1C_FIELDS = {
    "code": _field("id", "integer", "ADIF numeric DXCC entity code"),
    "name": _field("name", "string", "DXCC entity name"),
    "prefix": _field("prefix", "string", "Representative entity prefix"),
    "continent": _field("cont", "string", "Continent code"),
    "cq_zone": _field("cqz", "integer", "Default CQ zone supplied by the directory"),
    "itu_zone": _field("ituz", "integer", "Default ITU zone supplied by the directory"),
    "latitude": _field("lat", "number", "Representative entity latitude in decimal degrees"),
    "longitude": _field(
        "lon", "number", "Representative entity longitude in decimal degrees"
    ),
}

SATELLITE_FIELDS = {
    "name": _field(
        "name",
        "string",
        "Directory satellite name; use it as the comparison key with QSO satellite_name",
    ),
    "number": _field("number", "integer", "Satellite number supplied by QLog's directory"),
    "uplink": _field("uplink", "string", "Uplink information supplied by the directory"),
    "downlink": _field("downlink", "string", "Downlink information supplied by the directory"),
    "beacon": _field("beacon", "string", "Beacon information supplied by the directory"),
    "mode": _field(
        "mode", "string", "Satellite mode supplied by the directory; it is not a QSO matching key"
    ),
    "callsign": _field("callsign", "string", "Satellite callsign supplied by the directory"),
    "status": _field("status", "string", "Satellite status supplied by the directory"),
}

MEMBERSHIP_FIELDS = {
    "club": _field(
        "clubid", "string", "Club identifier from a membership list downloaded into QLog"
    ),
    "callsign": _field(
        "callsign", "string", "Member base callsign as stored in the downloaded list"
    ),
    "member_id": _field("member_id", "string", "Member identifier supplied by that club"),
    "valid_from": _compact_date_field(
        "valid_from",
        "Membership start date normalized from YYYYMMDD; use valid_from_state to distinguish "
        "an open boundary from malformed stored data",
    ),
    "valid_to": _compact_date_field(
        "valid_to",
        "Membership end date normalized from YYYYMMDD; use valid_to_state to distinguish an "
        "open boundary from malformed stored data",
    ),
    "valid_from_state": _compact_date_state_field(
        "valid_from",
        "Membership start-boundary state: open for blank, valid for a real YYYYMMDD date, "
        "or invalid for malformed non-empty data",
    ),
    "valid_to_state": _compact_date_state_field(
        "valid_to",
        "Membership end-boundary state: open for blank, valid for a real YYYYMMDD date, "
        "or invalid for malformed non-empty data",
    ),
}

MEMBERSHIP_CLUB_FIELDS = {
    "club": _field(
        "short_desc", "string", "Club identifier used by QLog membership records"
    ),
    "name": _field(
        "long_desc", "string", "Club name supplied by the membership directory"
    ),
    "source_file": _field("filename", "string", "Membership-list source filename"),
    "source_updated": _field(
        "last_update", "string", "Source update marker supplied by QLog's directory"
    ),
    "member_count": _field(
        "num_records", "integer", "Record count supplied by the membership directory"
    ),
}

CATALOGS: dict[CatalogName, CatalogDefinition] = {
    "pota": CatalogDefinition(
        "Parks on the Air reference directory",
        (CatalogSource("pota_directory", POTA_FIELDS, frozenset({"reference"})),),
        ("reference", "name", "active", "dxcc", "location", "grid"),
        "reference",
        ("pota_ref", "my_pota_ref"),
    ),
    "sota": CatalogDefinition(
        "Summits on the Air reference directory",
        (CatalogSource("sota_summits", SOTA_FIELDS, frozenset({"summit_code"})),),
        ("reference", "name", "association", "region", "points", "valid_from", "valid_to"),
        "reference",
        ("sota_ref", "my_sota_ref"),
    ),
    "wwff": CatalogDefinition(
        "World Wide Flora and Fauna reference directory",
        (CatalogSource("wwff_directory", WWFF_FIELDS, frozenset({"reference"})),),
        ("reference", "name", "program", "status", "dxcc", "grid"),
        "reference",
        ("wwff_ref", "my_wwff_ref"),
    ),
    "iota": CatalogDefinition(
        "Islands on the Air island-group directory",
        (CatalogSource("iota", IOTA_FIELDS, frozenset({"iotaid"})),),
        ("reference", "name"),
        "reference",
        ("iota", "my_iota"),
    ),
    "dxcc": CatalogDefinition(
        "DXCC entity directory",
        (
            CatalogSource(
                "dxcc_entities_clublog",
                DXCC_CLUBLOG_FIELDS,
                frozenset({"id", "name", "deleted", "start", "end"}),
            ),
            CatalogSource(
                "dxcc_entities_ad1c",
                DXCC_AD1C_FIELDS,
                frozenset({"id", "name"}),
                "reduced",
            ),
        ),
        ("code", "name", "prefix", "deleted", "continent", "valid_from", "valid_to"),
        "code",
        ("dxcc", "my_dxcc"),
    ),
    "satellite": CatalogDefinition(
        "Satellite directory stored by QLog; query metadata or compare satellite names in a scoped QSO population",
        (CatalogSource("sat_info", SATELLITE_FIELDS, frozenset({"name"})),),
        ("name", "number", "uplink", "downlink", "mode", "status"),
        "name",
        ("satellite_name",),
    ),
    "membership": CatalogDefinition(
        "Club membership records from lists downloaded into QLog; they do not represent all "
        "clubs and their dates are not award eligibility",
        (
            CatalogSource(
                "membership", MEMBERSHIP_FIELDS, frozenset({"callsign", "clubid"})
            ),
        ),
        ("club", "callsign", "member_id", "valid_from", "valid_to"),
        "callsign",
        (),
        ("club", "member_id", "valid_from", "valid_to"),
    ),
    "membership_clubs": CatalogDefinition(
        "Metadata for membership lists downloaded into QLog; use it to confirm a club is "
        "available before analyzing it",
        (
            CatalogSource(
                "membership_directory", MEMBERSHIP_CLUB_FIELDS, frozenset({"short_desc"})
            ),
        ),
        ("club", "name", "source_updated", "member_count"),
        "club",
        (),
    ),
}


class CatalogQuery:
    """Discover and query semantic reference catalogs in a QLog database."""

    def __init__(self, database: Database, qso: QsoQuery) -> None:
        self.database = database
        self.qso = qso

    @staticmethod
    def supported_catalog_names() -> list[str]:
        return list(CATALOG_NAMES)

    async def schema(self) -> dict[str, Any]:
        async with self.database.connect() as connection:
            available_qso_fields = await self.qso.available_field_names(connection)
            catalogs: dict[str, Any] = {}
            for name in CATALOG_NAMES:
                resolved = await self._find_catalog(connection, name)
                if resolved is None:
                    continue
                definition = resolved.definition
                qso_fields = [name for name in definition.qso_fields if name in available_qso_fields]
                catalogs[name] = {
                    "description": definition.description,
                    "source_capability": resolved.capability,
                    "fields": {
                        field_name: {
                            "type": field.value_type,
                            "description": field.description,
                            "operators": [operator.value for operator in field.operators()],
                        }
                        for field_name, field in resolved.fields.items()
                    },
                    "default_fields": [
                        field for field in definition.default_fields if field in resolved.fields
                    ],
                    "default_order": [
                        {"field": field, "direction": "asc"}
                        for field in (definition.key_field, *definition.tie_breaker_fields)
                    ],
                    "max_limit": MAX_LIMIT,
                    "compatible_qso_fields": qso_fields,
                    "comparison_key": {
                        "field": definition.key_field,
                        "type": resolved.fields[definition.key_field].value_type,
                        "description": (
                            "Catalog field compared with the selected compatible QSO field."
                        ),
                    },
                }

        used_operators = {
            operator
            for catalog in catalogs.values()
            for field in catalog["fields"].values()
            for operator in field["operators"]
        }
        return {
            "catalogs": catalogs,
            "source_capability_semantics": {
                "full": "All semantic fields defined for the preferred catalog source exist.",
                "reduced": (
                    "Only the advertised semantic fields are available; do not assume missing "
                    "fields or infer their values."
                ),
            },
            "compatible_qso_fields_semantics": (
                "Available QSO fields whose recorded references or codes can be compared with "
                "this catalog. Read the QSO schema's side, paired_field, cardinality, and "
                "list_semantics before choosing between contacted- and logging-station fields. "
                "Compatibility does not imply validity, confirmation, or award credit."
            ),
            "filter_operator_semantics": {
                operator.value: description
                for operator, description in FILTER_OPERATOR_DESCRIPTIONS.items()
                if operator.value in used_operators
            },
            "query": {
                "default_limit": 100,
                "max_limit": MAX_LIMIT,
                "pagination": "Use page.next_offset only while page.has_more is true.",
            },
            "match_qso": {
                "relations": {
                    relation.value: description
                    for relation, description in MATCH_RELATION_DESCRIPTIONS.items()
                },
                "qso_only_fields": {
                    "key": (
                        "Normalized non-empty semantic QSO key absent from the filtered "
                        "catalog; its meaning and type come from the catalog's comparison_key."
                    ),
                    "qso_count": (
                        "Number of filtered QSOs containing this key; a repeated equal list "
                        "item within one QSO counts once."
                    ),
                },
                "summary_fields": {
                    "catalog_values": (
                        "Distinct non-empty keys in the catalog after catalog_filters."
                    ),
                    "matched_values": (
                        "Distinct filtered catalog keys also present in the scoped and "
                        "filtered QSO key set."
                    ),
                    "not_matched_values": (
                        "Distinct filtered catalog keys absent from the scoped and filtered "
                        "QSO key set."
                    ),
                    "qso_only_values": (
                        "Distinct non-empty scoped and filtered QSO keys absent from the "
                        "filtered catalog."
                    ),
                },
                "item_semantics": {
                    "matched": "Catalog rows for keys present in the QSO key set.",
                    "not_matched": "Catalog rows for keys absent from the QSO key set.",
                    "qso_only": "Objects containing only key and qso_count, not QSO rows.",
                },
                "processing": (
                    "Build the scoped QSO population, apply qso_filters, normalize and "
                    "deduplicate its non-empty keys; independently apply catalog_filters and "
                    "deduplicate catalog keys; compare the two sets; calculate the complete "
                    "summary; then sort and paginate only the detail items. Text keys compare "
                    "case-insensitively. No award or contest rule is inferred."
                ),
            },
        }

    async def query(
        self,
        catalog: str,
        filters: FilterGroup | None,
        fields: list[str] | None,
        sort: list[CatalogSort] | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        self._validate_page(limit, offset)
        if catalog not in CATALOGS:
            raise InvalidQueryError(f"Unknown catalog: {catalog}")

        async with self.database.connect() as connection:
            resolved = await self._find_catalog(connection, catalog)
            if resolved is None:
                raise IncompatibleDatabaseError(
                    f"Catalog {catalog!r} is unavailable in this QLog database schema"
                )
            selected_names = self._resolve_fields(resolved, fields)
            parameters: list[Any] = []
            where_sql = (
                self._compile_group(resolved, filters, parameters)
                if filters is not None
                else "1 = 1"
            )
            select_sql = ", ".join(
                f'{resolved.fields[name].expression()} AS "{name}"' for name in selected_names
            )
            order_sql = self._compile_sort(resolved, sort)
            sql = (
                f'SELECT {select_sql} FROM "{resolved.source.table}" AS d '
                f"WHERE {where_sql} ORDER BY {order_sql} LIMIT ? OFFSET ?"
            )
            parameters.extend((limit + 1, offset))
            rows = await self._execute_sql(connection, sql, parameters)

        has_more = len(rows) > limit
        items = [self._serialize_row(row, selected_names, resolved.fields) for row in rows[:limit]]
        return {
            "catalog": catalog,
            "items": items,
            "fields": selected_names,
            "page": {
                "limit": limit,
                "offset": offset,
                "returned": len(items),
                "has_more": has_more,
                "next_offset": offset + limit if has_more else None,
            },
        }

    async def match_qso(
        self,
        catalog: str,
        qso_field: str,
        scope: LogScope,
        qso_filters: FilterGroup | None,
        catalog_filters: FilterGroup | None,
        relation: MatchRelation,
        fields: list[str] | None,
        sort: list[CatalogMatchSort] | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        """Compare catalog keys with distinct values in a scoped QSO population."""
        self._validate_page(limit, offset)
        if catalog not in CATALOGS:
            raise InvalidQueryError(f"Unknown catalog: {catalog}")

        relation = MatchRelation(relation)
        async with self.database.connect() as connection:
            resolved = await self._find_catalog(connection, catalog)
            if resolved is None:
                raise IncompatibleDatabaseError(
                    f"Catalog {catalog!r} is unavailable in this QLog database schema"
                )
            if not resolved.definition.qso_fields:
                suggestion = (
                    "first confirm the club exists in membership_clubs, then use "
                    "member_clubs_at_qso_date or member_clubs_in_directory with qso.query "
                    "or qso.aggregate"
                    if catalog == "membership"
                    else "use catalog.query"
                )
                raise InvalidQueryError(
                    f"Catalog {catalog!r} does not support catalog.match_qso; {suggestion}"
                )
            if qso_field not in resolved.definition.qso_fields:
                compatible = ", ".join(resolved.definition.qso_fields)
                raise InvalidQueryError(
                    f"QSO field {qso_field!r} cannot be matched with catalog {catalog!r}; "
                    f"use one of: {compatible}"
                )

            selected_names = self._resolve_match_fields(resolved, relation, fields)
            order_sql, sort_names = self._compile_match_sort(resolved, relation, sort)
            catalog_names = list(
                dict.fromkeys(
                    [
                        *(selected_names if relation != MatchRelation.QSO_ONLY else []),
                        *sort_names,
                        resolved.definition.key_field,
                    ]
                )
            )
            if relation == MatchRelation.QSO_ONLY:
                catalog_names = [resolved.definition.key_field]

            qso_ctes, parameters = await self.qso.compile_value_set(
                connection, scope, qso_filters, qso_field
            )
            catalog_parameters: list[Any] = []
            catalog_where = (
                self._compile_group(resolved, catalog_filters, catalog_parameters)
                if catalog_filters is not None
                else "1 = 1"
            )
            parameters.extend(catalog_parameters)

            key_name = resolved.definition.key_field
            key_field = resolved.fields[key_name]
            key_expression = key_field.expression()
            if key_field.value_type == "string":
                key_expression = f"NULLIF(TRIM(CAST({key_expression} AS TEXT)), '')"
            key_collation = " COLLATE NOCASE" if key_field.value_type == "string" else ""
            join_sql = (
                'c."_key" COLLATE NOCASE = q."_key" COLLATE NOCASE'
                if key_field.value_type == "string"
                else 'c."_key" = q."_key"'
            )

            catalog_columns = ", ".join(
                f'{resolved.fields[name].expression()} AS "{name}"'
                for name in catalog_names
            )
            grouped_columns = ", ".join(
                f'MIN("{name}") AS "{name}"' for name in catalog_names
            )
            ctes = [
                *qso_ctes,
                (
                    '"_catalog_rows" AS ('
                    f'SELECT {key_expression} AS "_key", {catalog_columns} '
                    f'FROM "{resolved.source.table}" AS d WHERE ({catalog_where}) '
                    f'AND {key_expression} IS NOT NULL)'
                ),
                (
                    '"_catalog_values" AS ('
                    f'SELECT MIN("_key") AS "_key", {grouped_columns} '
                    f'FROM "_catalog_rows" GROUP BY "_key"{key_collation})'
                ),
                (
                    '"_summary" AS ('
                    'SELECT (SELECT COUNT(*) FROM "_catalog_values") AS "catalog_values", '
                    f'(SELECT COUNT(*) FROM "_catalog_values" AS c JOIN "_qso_values" AS q ON {join_sql}) '
                    'AS "matched_values", '
                    f'(SELECT COUNT(*) FROM "_catalog_values" AS c LEFT JOIN "_qso_values" AS q ON {join_sql} '
                    'WHERE q."_key" IS NULL) AS "not_matched_values", '
                    f'(SELECT COUNT(*) FROM "_qso_values" AS q LEFT JOIN "_catalog_values" AS c ON {join_sql} '
                    'WHERE c."_key" IS NULL) AS "qso_only_values")'
                ),
                self._match_rows_cte(
                    relation,
                    selected_names if relation == MatchRelation.QSO_ONLY else catalog_names,
                    join_sql,
                ),
                (
                    f'"_page" AS (SELECT * FROM "_relation_rows" ORDER BY {order_sql} '
                    'LIMIT ? OFFSET ?)'
                ),
            ]
            parameters.extend((limit + 1, offset))
            item_columns = ", ".join(
                f'p."{name}" AS "_item_{name}"' for name in selected_names
            )
            sql = (
                "WITH RECURSIVE "
                + ", ".join(ctes)
                + ' SELECT s."catalog_values", s."matched_values", '
                + 's."not_matched_values", s."qso_only_values", '
                + f'p."_present" AS "_item_present", {item_columns} '
                + 'FROM "_summary" AS s LEFT JOIN "_page" AS p ON 1 = 1 '
                + f"ORDER BY {order_sql}"
            )
            rows = await self._execute_sql(connection, sql, parameters)

        detail_rows = [row for row in rows if row["_item_present"] is not None]
        has_more = len(detail_rows) > limit
        items = [
            self._serialize_match_row(row, selected_names, resolved, relation)
            for row in detail_rows[:limit]
        ]
        summary = rows[0]
        return {
            "relation": relation.value,
            "summary": {
                "catalog_values": summary["catalog_values"],
                "matched_values": summary["matched_values"],
                "not_matched_values": summary["not_matched_values"],
                "qso_only_values": summary["qso_only_values"],
            },
            "items": items,
            "fields": selected_names,
            "effective_scope": scope.model_dump(
                mode="json", exclude_defaults=True, exclude_none=True
            ),
            "page": {
                "limit": limit,
                "offset": offset,
                "returned": len(items),
                "has_more": has_more,
                "next_offset": offset + limit if has_more else None,
            },
        }

    async def match_membership_qso(
        self,
        club: str,
        scope: LogScope,
        qso_filters: FilterGroup | None,
        member_as_of: date | None,
        membership_basis: MembershipMatchBasis,
        relation: MembershipMatchRelation,
        sort: list[MembershipMatchSort] | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        """Match one downloaded club roster with QSOs during recorded membership periods."""
        self._validate_page(limit, offset)
        relation = MembershipMatchRelation(relation)
        membership_basis = MembershipMatchBasis(membership_basis)
        if not club.strip():
            raise InvalidQueryError("club must not be empty")
        if member_as_of is not None and membership_basis == MembershipMatchBasis.DIRECTORY_SNAPSHOT:
            raise InvalidQueryError(
                "member_as_of is unavailable with directory_snapshot; that basis uses the "
                "stored local roster without interpreting membership dates"
            )

        async with self.database.connect() as connection:
            club_catalog = await self._find_catalog(connection, "membership_clubs")
            membership_catalog = await self._find_catalog(connection, "membership")
            if club_catalog is None or membership_catalog is None:
                raise IncompatibleDatabaseError(
                    "Membership matching requires downloaded membership lists and their metadata"
                )
            club_key = club_catalog.fields["club"].expression()
            club_rows = await self._execute_sql(
                connection,
                f'SELECT {club_key} AS "_club" FROM "{club_catalog.source.table}" AS d '
                f"WHERE {club_key} COLLATE NOCASE = ? LIMIT 1",
                [club],
            )
            if not club_rows:
                raise InvalidQueryError(
                    "Membership list is not downloaded for this club; membership cannot be "
                    "inferred from its absence"
                )
            canonical_club = club_rows[0]["_club"]

            membership_columns = await self._table_columns(connection, "membership")
            required_membership = {"callsign", "clubid", "valid_from", "valid_to"}
            if missing := required_membership - membership_columns:
                raise IncompatibleDatabaseError(
                    "Membership matching is unavailable; membership is missing: "
                    + ", ".join(sorted(missing))
                )
            columns, source, qso_where, qso_parameters = await self.qso.compile_scoped_source(
                connection, scope, qso_filters
            )
            required_qso = {
                "contacts_autovalue.contactid",
                "contacts_autovalue.base_callsign",
            }
            if missing := required_qso - columns:
                raise IncompatibleDatabaseError(
                    "Membership matching requires QLog base callsigns; contacts_autovalue is "
                    "missing: "
                    + ", ".join(sorted(missing))
                )

            from_value = 'TRIM(CAST(m."valid_from" AS TEXT))'
            to_value = 'TRIM(CAST(m."valid_to" AS TEXT))'
            from_date = compact_date_expression(from_value)
            to_date = compact_date_expression(to_value)
            member_source = (
                '"_membership_source" AS ('
                'SELECT m."callsign" AS "_callsign", '
                f'{from_date} AS "_from_date", {to_date} AS "_to_date", '
                f"NULLIF({from_value}, '') IS NULL AS \"_from_open\", "
                f"NULLIF({to_value}, '') IS NULL AS \"_to_open\" "
                'FROM membership AS m '
                'WHERE m."clubid" = ? '
                'AND NULLIF(TRIM(CAST(m."callsign" AS TEXT)), \'\') IS NOT NULL)'
            )
            usable_dates = (
                '("_from_open" OR "_from_date" IS NOT NULL) '
                'AND ("_to_open" OR "_to_date" IS NOT NULL)'
            )
            request_ctes: list[str] = []
            parameters: list[Any] = []
            member_source_sql = member_source
            member_rows_source = '"_membership_source"'
            if member_as_of is not None:
                request_ctes.append('"_request" AS (SELECT ? AS "_as_of")')
                parameters.append(member_as_of.isoformat())
                member_rows_source += ' CROSS JOIN "_request" AS r'
                usable_dates += (
                    ' AND r."_as_of" >= COALESCE("_from_date", '
                    'CASE WHEN "_from_open" THEN r."_as_of" END)'
                    ' AND r."_as_of" <= COALESCE("_to_date", '
                    'CASE WHEN "_to_open" THEN r."_as_of" END)'
                )
            parameters.append(canonical_club)
            membership_where = (
                usable_dates if membership_basis == MembershipMatchBasis.QSO_DATE else "1 = 1"
            )
            qso_rows = (
                '"_qso_rows" AS ('
                'SELECT c."id" AS "_contact_id", DATE(c."start_time") AS "_qso_date", '
                "strftime('%Y-%m-%dT%H:%M:%SZ', c.\"start_time\") AS \"_datetime\", "
                'a."base_callsign" AS "_callsign" '
                f'FROM {source} WHERE ({qso_where}) '
                'AND NULLIF(TRIM(CAST(a."base_callsign" AS TEXT)), \'\') IS NOT NULL '
                'AND DATE(c."start_time") IS NOT NULL)'
            )
            match_source, match_callsign = (
                ('"_membership_rows" AS r', 'r."_callsign"')
                if membership_basis == MembershipMatchBasis.QSO_DATE
                else ('"_roster" AS r', 'r."callsign"')
            )
            matching = (
                '"_matching_qsos" AS ('
                f'SELECT DISTINCT {match_callsign} AS "_callsign", '
                'q."_contact_id", q."_datetime" '
                f'FROM {match_source} JOIN "_qso_rows" AS q '
                f'ON q."_callsign" = {match_callsign} '
                + (
                    'AND q."_qso_date" >= COALESCE(r."_from_date", '
                    'CASE WHEN r."_from_open" THEN q."_qso_date" END) '
                    'AND q."_qso_date" <= COALESCE(r."_to_date", '
                    'CASE WHEN r."_to_open" THEN q."_qso_date" END)'
                    if membership_basis == MembershipMatchBasis.QSO_DATE
                    else ""
                )
                + ")"
            )
            invalid_rows = (
                '"_invalid_rows" AS ('
                'SELECT COUNT(*) AS "_count" FROM membership AS m '
                'WHERE m."clubid" = ? '
                f"AND ((NULLIF({from_value}, '') IS NOT NULL AND ({from_date}) IS NULL) "
                f"OR (NULLIF({to_value}, '') IS NOT NULL AND ({to_date}) IS NULL)))"
            )
            relation_source = (
                '"_worked"'
                if relation == MembershipMatchRelation.WORKED
                else '"_roster" AS r LEFT JOIN "_worked" AS w '
                'ON w."callsign" = r."callsign" WHERE w."callsign" IS NULL'
            )
            relation_columns = (
                '"callsign", "qso_count", "first_qso", "last_qso"'
                if relation == MembershipMatchRelation.WORKED
                else 'r."callsign" AS "callsign", 0 AS "qso_count", '
                'NULL AS "first_qso", NULL AS "last_qso"'
            )
            excluded_invalid_sql = (
                '(SELECT "_count" FROM "_invalid_rows")'
                if membership_basis == MembershipMatchBasis.QSO_DATE
                else "0"
            )
            order_sql = self._compile_membership_match_sort(sort)
            ctes = [
                *request_ctes,
                member_source_sql,
                f'"_membership_rows" AS (SELECT * FROM {member_rows_source} WHERE {membership_where})',
                '"_roster" AS (SELECT DISTINCT "_callsign" AS "callsign" FROM "_membership_rows")',
                qso_rows,
                matching,
                (
                    '"_worked" AS (SELECT "_callsign" AS "callsign", COUNT(*) AS '
                    '"qso_count", MIN("_datetime") AS "first_qso", MAX("_datetime") '
                    'AS "last_qso" FROM "_matching_qsos" GROUP BY "_callsign")'
                ),
                invalid_rows,
                (
                    '"_summary" AS (SELECT '
                    '(SELECT COUNT(*) FROM "_roster") AS "member_callsigns", '
                    '(SELECT COUNT(*) FROM "_worked") AS "worked_callsigns", '
                    '(SELECT COUNT(*) FROM "_roster") - (SELECT COUNT(*) FROM "_worked") '
                    'AS "not_worked_callsigns", '
                    '(SELECT COUNT(*) FROM "_matching_qsos") AS "matching_qsos", '
                    '(SELECT "_count" FROM "_invalid_rows") AS "invalid_membership_records", '
                    f'{excluded_invalid_sql} AS '
                    '"excluded_invalid_membership_records")'
                ),
                (
                    f'"_relation_rows" AS (SELECT 1 AS "_present", {relation_columns} '
                    f'FROM {relation_source})'
                ),
                (
                    f'"_page" AS (SELECT * FROM "_relation_rows" ORDER BY {order_sql} '
                    'LIMIT ? OFFSET ?)'
                ),
            ]
            parameters.extend(qso_parameters)
            parameters.append(canonical_club)
            parameters.extend((limit + 1, offset))
            sql = (
                "WITH "
                + ", ".join(ctes)
                + ' SELECT s.*, p."_present", p."callsign", p."qso_count", '
                'p."first_qso", p."last_qso" FROM "_summary" AS s '
                'LEFT JOIN "_page" AS p ON 1 = 1 '
                f"ORDER BY {order_sql}"
            )
            rows = await self._execute_sql(connection, sql, parameters)

        detail_rows = [row for row in rows if row["_present"] is not None]
        has_more = len(detail_rows) > limit
        summary = rows[0]
        return {
            "club": club,
            "membership_basis": membership_basis.value,
            "member_as_of": member_as_of.isoformat() if member_as_of else None,
            "relation": relation.value,
            "summary": {
                "member_callsigns": summary["member_callsigns"],
                "worked_callsigns": summary["worked_callsigns"],
                "not_worked_callsigns": summary["not_worked_callsigns"],
                "matching_qsos": summary["matching_qsos"],
                "invalid_membership_records": summary["invalid_membership_records"],
                "excluded_invalid_membership_records": summary[
                    "excluded_invalid_membership_records"
                ],
            },
            "items": [
                {
                    "callsign": row["callsign"],
                    "qso_count": row["qso_count"],
                    "first_qso": row["first_qso"],
                    "last_qso": row["last_qso"],
                }
                for row in detail_rows[:limit]
            ],
            "effective_scope": scope.model_dump(
                mode="json", exclude_defaults=True, exclude_none=True
            ),
            "page": {
                "limit": limit,
                "offset": offset,
                "returned": min(len(detail_rows), limit),
                "has_more": has_more,
                "next_offset": offset + limit if has_more else None,
            },
        }

    @staticmethod
    def _compile_membership_match_sort(
        sort: list[MembershipMatchSort] | None,
    ) -> str:
        requested = sort or [MembershipMatchSort(field="callsign")]
        parts: list[str] = []
        fields: set[str] = set()
        for item in requested:
            if item.field in fields:
                continue
            parts.append(f'"{item.field}" {item.direction.value.upper()}')
            fields.add(item.field)
        if "callsign" not in fields:
            parts.append('"callsign" ASC')
        return ", ".join(parts)

    @staticmethod
    def _resolve_match_fields(
        resolved: ResolvedCatalog,
        relation: MatchRelation,
        fields: list[str] | None,
    ) -> list[str]:
        if relation != MatchRelation.QSO_ONLY:
            return CatalogQuery._resolve_fields(resolved, fields)
        requested = ["key", "qso_count"] if fields is None else list(dict.fromkeys(fields))
        if set(requested) != {"key", "qso_count"}:
            raise InvalidQueryError(
                "qso_only fields must contain both 'key' and 'qso_count'"
            )
        return requested

    @staticmethod
    def _compile_match_sort(
        resolved: ResolvedCatalog,
        relation: MatchRelation,
        sort: list[CatalogMatchSort] | None,
    ) -> tuple[str, list[str]]:
        default_field = "key" if relation == MatchRelation.QSO_ONLY else resolved.definition.key_field
        requested = sort or [CatalogMatchSort(field=default_field)]
        parts: list[str] = []
        fields: list[str] = []
        for item in requested:
            if item.field in fields:
                continue
            if relation == MatchRelation.QSO_ONLY:
                if item.field not in {"key", "qso_count"}:
                    raise InvalidQueryError(
                        "qso_only sort field must be 'key' or 'qso_count'"
                    )
            else:
                CatalogQuery._require_field(resolved, item.field, "sort field")
            parts.append(f'"{item.field}" {item.direction.value.upper()}')
            fields.append(item.field)
        if default_field not in fields:
            parts.append(f'"{default_field}" ASC')
        return ", ".join(parts), fields

    @staticmethod
    def _match_rows_cte(
        relation: MatchRelation,
        selected_names: list[str],
        join_sql: str,
    ) -> str:
        if relation == MatchRelation.QSO_ONLY:
            return (
                '"_relation_rows" AS (SELECT 1 AS "_present", q."_key" AS "key", '
                'q."_qso_count" AS "qso_count" FROM "_qso_values" AS q '
                f'LEFT JOIN "_catalog_values" AS c ON {join_sql} WHERE c."_key" IS NULL)'
            )
        columns = ", ".join(f'c."{name}" AS "{name}"' for name in selected_names)
        if relation == MatchRelation.MATCHED:
            source = f'JOIN "_qso_values" AS q ON {join_sql}'
            condition = ""
        else:
            source = f'LEFT JOIN "_qso_values" AS q ON {join_sql}'
            condition = ' WHERE q."_key" IS NULL'
        return (
            f'"_relation_rows" AS (SELECT 1 AS "_present", {columns} '
            f'FROM "_catalog_values" AS c {source}{condition})'
        )

    @staticmethod
    def _serialize_match_row(
        row: aiosqlite.Row,
        fields: list[str],
        resolved: ResolvedCatalog,
        relation: MatchRelation,
    ) -> dict[str, Any]:
        result = {name: row[f"_item_{name}"] for name in fields}
        if relation != MatchRelation.QSO_ONLY:
            for name in fields:
                if resolved.fields[name].value_type == "boolean" and result[name] is not None:
                    result[name] = bool(result[name])
        return result

    async def _find_catalog(
        self, connection: aiosqlite.Connection, name: CatalogName
    ) -> ResolvedCatalog | None:
        definition = CATALOGS[name]
        for source in definition.sources:
            columns = await self._table_columns(connection, source.table)
            if not source.required_columns.issubset(columns):
                continue
            fields = {
                field_name: field
                for field_name, field in source.fields.items()
                if field.column in columns
            }
            capability = (
                source.capability
                if len(fields) == len(source.fields)
                else "reduced"
            )
            return ResolvedCatalog(name, definition, source, fields, capability)
        return None

    @staticmethod
    async def _table_columns(
        connection: aiosqlite.Connection, table: str
    ) -> set[str]:
        async with connection.execute(f'PRAGMA table_info("{table}")') as cursor:
            return {row["name"] for row in await cursor.fetchall()}

    @staticmethod
    def _validate_page(limit: int, offset: int) -> None:
        if not 1 <= limit <= MAX_LIMIT:
            raise InvalidQueryError(f"limit must be between 1 and {MAX_LIMIT}")
        if offset < 0:
            raise InvalidQueryError("offset must be zero or greater")

    @staticmethod
    def _resolve_fields(
        resolved: ResolvedCatalog, fields: list[str] | None
    ) -> list[str]:
        requested = (
            [name for name in resolved.definition.default_fields if name in resolved.fields]
            if fields is None
            else fields
        )
        if not requested:
            raise InvalidQueryError("fields must contain at least one semantic field")

        result: list[str] = []
        for name in requested:
            if name in result:
                continue
            CatalogQuery._require_field(resolved, name, "field")
            result.append(name)
        return result

    def _compile_group(
        self,
        resolved: ResolvedCatalog,
        group: FilterGroup,
        parameters: list[Any],
    ) -> str:
        parts = [
            self._compile_condition(resolved, condition, parameters)
            for condition in group.conditions
        ]
        parts.extend(self._compile_group(resolved, child, parameters) for child in group.groups)
        expression = f" {group.logic.value.upper()} ".join(f"({part})" for part in parts)
        expression = expression or "1 = 1"
        return f"NOT ({expression})" if group.negate else expression

    def _compile_condition(
        self,
        resolved: ResolvedCatalog,
        condition: FilterCondition,
        parameters: list[Any],
    ) -> str:
        field = self._require_field(resolved, condition.field, "filter field")
        operator = condition.op
        if operator not in field.operators():
            raise InvalidQueryError(
                f"Operator {operator.value!r} is not supported for catalog field "
                f"{condition.field!r}"
            )

        expression = field.comparison_expression()
        value = condition.value
        if operator == FilterOperator.IS_NULL:
            return f"{expression} IS NULL"
        if operator == FilterOperator.IS_NOT_NULL:
            return f"{expression} IS NOT NULL"
        if operator in {FilterOperator.IS_EMPTY, FilterOperator.IS_NOT_EMPTY}:
            empty = f"NULLIF(TRIM(CAST({field.expression()} AS TEXT)), '')"
            keyword = "IS NULL" if operator == FilterOperator.IS_EMPTY else "IS NOT NULL"
            return f"{empty} {keyword}"
        if operator == FilterOperator.EQ and value is None:
            return f"{expression} IS NULL"
        if operator == FilterOperator.NEQ and value is None:
            return f"{expression} IS NOT NULL"

        if operator in {FilterOperator.IN, FilterOperator.NOT_IN}:
            if not isinstance(value, list) or not value:
                raise InvalidQueryError(f"{operator.value} requires a non-empty value list")
            parameters.extend(value)
            keyword = "IN" if operator == FilterOperator.IN else "NOT IN"
            return f"{expression} {keyword} ({', '.join('?' for _ in value)})"

        if operator == FilterOperator.BETWEEN:
            if not isinstance(value, list) or len(value) != 2:
                raise InvalidQueryError("between requires exactly two values")
            parameters.extend(value)
            return f"{expression} BETWEEN ? AND ?"

        if isinstance(value, list) or value is None:
            raise InvalidQueryError(f"{operator.value} requires one scalar value")

        if operator in TEXT_OPERATORS:
            if not isinstance(value, str):
                raise InvalidQueryError(f"{operator.value} requires a string field and value")
            escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            if operator == FilterOperator.CONTAINS:
                escaped = f"%{escaped}%"
            elif operator == FilterOperator.STARTS_WITH:
                escaped = f"{escaped}%"
            else:
                escaped = f"%{escaped}"
            parameters.append(escaped)
            return f"{expression} LIKE ? ESCAPE '\\'"

        sql_operator = {
            FilterOperator.EQ: "=",
            FilterOperator.NEQ: "<>",
            FilterOperator.GT: ">",
            FilterOperator.GTE: ">=",
            FilterOperator.LT: "<",
            FilterOperator.LTE: "<=",
        }.get(operator)
        if sql_operator is None:
            raise InvalidQueryError(f"Unsupported filter operator: {operator.value}")
        parameters.append(value)
        return f"{expression} {sql_operator} ?"

    @staticmethod
    def _compile_sort(
        resolved: ResolvedCatalog, sort: list[CatalogSort] | None
    ) -> str:
        requested = sort or [CatalogSort(field=resolved.definition.key_field)]
        parts: list[str] = []
        sorted_fields: set[str] = set()
        for item in requested:
            if item.field in sorted_fields:
                continue
            field = CatalogQuery._require_field(resolved, item.field, "sort field")
            parts.append(f"{field.expression()} {item.direction.value.upper()}")
            sorted_fields.add(item.field)
        for field_name in (
            resolved.definition.key_field,
            *resolved.definition.tie_breaker_fields,
        ):
            if field_name not in sorted_fields:
                parts.append(f"{resolved.fields[field_name].expression()} ASC")
        return ", ".join(parts)

    @staticmethod
    def _require_field(
        resolved: ResolvedCatalog, name: str, purpose: str
    ) -> CatalogField:
        field = resolved.fields.get(name)
        if field is not None:
            return field
        if name in resolved.definition.field_names:
            raise IncompatibleDatabaseError(
                f"Catalog field {name!r} is unavailable for {resolved.name!r} in this "
                "QLog database schema"
            )
        raise InvalidQueryError(f"Unknown catalog {purpose}: {name}")

    @staticmethod
    def _serialize_row(
        row: aiosqlite.Row,
        fields: list[str],
        definitions: dict[str, CatalogField],
    ) -> dict[str, Any]:
        result = {name: row[name] for name in fields}
        for name in fields:
            if definitions[name].value_type == "boolean" and result[name] is not None:
                result[name] = bool(result[name])
        return result

    @staticmethod
    async def _execute_sql(
        connection: aiosqlite.Connection,
        sql: str,
        parameters: list[Any],
    ) -> list[aiosqlite.Row]:
        started = time.perf_counter()
        try:
            async with connection.execute(sql, parameters) as cursor:
                rows = await cursor.fetchall()
        except Exception as error:
            record_sql(
                sql,
                parameters,
                (time.perf_counter() - started) * 1000,
                returned_rows=None,
                error=error,
            )
            raise
        record_sql(
            sql,
            parameters,
            (time.perf_counter() - started) * 1000,
            returned_rows=len(rows),
        )
        return rows
