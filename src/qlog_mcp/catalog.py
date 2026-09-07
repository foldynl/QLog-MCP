"""Read-only semantic queries over QLog reference catalogs."""

from __future__ import annotations

import time
from dataclasses import dataclass
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
)
from .qso import LogScope, QsoQuery
from .usage_log import record_sql

CatalogName = Literal["pota", "sota", "wwff", "iota", "dxcc"]
CatalogValueType = Literal["string", "integer", "number", "boolean", "date"]
CATALOG_NAMES: tuple[CatalogName, ...] = ("pota", "sota", "wwff", "iota", "dxcc")
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
            contacts_columns = await self._table_columns(connection, "contacts")
            catalogs: dict[str, Any] = {}
            for name in CATALOG_NAMES:
                resolved = await self._find_catalog(connection, name)
                if resolved is None:
                    continue
                definition = resolved.definition
                qso_fields = [name for name in definition.qso_fields if name in contacts_columns]
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
                        {"field": definition.key_field, "direction": "asc"}
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
        default_order = resolved.definition.key_field
        if default_order not in sorted_fields:
            parts.append(f"{resolved.fields[default_order].expression()} ASC")
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
