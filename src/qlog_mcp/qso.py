"""Read-only semantic queries over QLog's QSO tables."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date
from enum import Enum
from typing import Any, Literal

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .database import Database
from .errors import IncompatibleDatabaseError, InvalidQueryError
from .filters import (
    BOOLEAN_OPERATORS,
    COMPARISON_OPERATORS,
    FILTER_OPERATOR_DESCRIPTIONS,
    LIST_OPERATORS,
    PRESENCE_OPERATORS,
    TEXT_OPERATORS,
    FilterCondition,
    FilterGroup,
    FilterOperator,
    Scalar,
    SortDirection,
)
from .usage_log import record_sql

FieldCardinality = Literal["one", "many"]
FieldSide = Literal["contacted", "logging", "operator", "qso"]

QSO_UPLOAD_STATUS_VALUES = {
    "Y": "Uploaded and accepted by the online service",
    "N": "Do not upload to the online service",
    "M": "Modified after a previous upload",
}


class HavingOperator(str, Enum):
    """Comparison supported between an aggregate metric and a scalar threshold."""

    EQ = "eq"
    NEQ = "neq"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"


class GroupInterval(str, Enum):
    """UTC calendar interval used to group a date or datetime field."""

    MINUTE = "minute"
    HOUR = "hour"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"


GROUP_INTERVAL_DESCRIPTIONS = {
    GroupInterval.MINUTE: (
        "Fixed-size UTC minute bucket aligned to the Unix epoch, returned as "
        "YYYY-MM-DDTHH:MM:00Z; size is required."
    ),
    GroupInterval.HOUR: "UTC hour as YYYY-MM-DDTHH:00:00Z; datetime fields only.",
    GroupInterval.DAY: "UTC calendar day as YYYY-MM-DD.",
    GroupInterval.WEEK: "UTC week represented by its Monday date as YYYY-MM-DD.",
    GroupInterval.MONTH: "UTC calendar month as YYYY-MM.",
    GroupInterval.QUARTER: "UTC calendar quarter as YYYY-Q1 through YYYY-Q4.",
    GroupInterval.YEAR: "UTC calendar year as YYYY.",
}


class AggregateFunction(str, Enum):
    """Server-side function calculated over the QSOs in each group."""

    COUNT = "count"
    DISTINCT_COUNT = "distinct_count"
    SUM = "sum"
    AVG = "avg"
    MIN = "min"
    MAX = "max"


AGGREGATE_FUNCTION_DESCRIPTIONS = {
    AggregateFunction.COUNT: "Count QSO rows; field must be omitted",
    AggregateFunction.DISTINCT_COUNT: (
        "Count distinct non-empty values from one field or typed tuples from fields; set "
        "explode=true on one list field to count its semantic items; strings are "
        "case-insensitive"
    ),
    AggregateFunction.SUM: "Sum non-empty values of a numeric field",
    AggregateFunction.AVG: "Average non-empty values of a numeric field",
    AggregateFunction.MIN: "Minimum non-empty numeric, date, or datetime value",
    AggregateFunction.MAX: "Maximum non-empty numeric, date, or datetime value",
}


class StationScope(str, Enum):
    """Method used to select the logging station's QSOs."""

    CALLSIGN = "callsign"
    PROFILE = "profile"
    ALL = "all"


class StationCallsignSelector(BaseModel):
    """Select one local station callsign, optionally restricted to a Maidenhead locator."""

    callsign: str = Field(
        min_length=1,
        description="Local station callsign, for example OK1MLG.",
    )
    grid: str | None = Field(
        default=None,
        min_length=2,
        description=(
            "Optional local station Maidenhead locator. "
            "If specified, only QSOs made with this callsign from this grid are included. "
            "If omitted, all QSOs for this callsign are included regardless of grid. "
            "Use a grid returned by qlog.get_context when selecting a specific location."
        ),
    )


class LogScope(BaseModel):
    """Station and optional date/operator restrictions for a QSO operation."""

    station_scope: StationScope = Field(
        description=(
            "How to select the logging station: 'callsign' uses station_callsigns, "
            "'profile' uses station_profile_names, and 'all' includes the entire log. "
            "Use 'all' only after the user explicitly chooses it."
        ),
    )
    station_callsigns: list[StationCallsignSelector] = Field(
        default_factory=list,
        description=(
            "Required for station_scope='callsign'. Use callsigns and optional grids "
            "returned by qlog.get_context."
        ),
    )
    operator_callsigns: list[str] = Field(
        default_factory=list,
        description=(
            "Optional case-insensitive restriction to ADIF OPERATOR: the person operating "
            "the logging station. This is combined with the station and date restrictions."
        ),
    )
    station_profile_names: list[str] = Field(
        default_factory=list,
        description=(
            "Required for station_scope='profile'. Use names returned by qlog.get_context."
        ),
    )
    date_from: date | None = Field(
        default=None,
        description="Optional earliest UTC QSO start date, inclusive.",
    )
    date_to: date | None = Field(
        default=None,
        description="Optional latest UTC QSO start date, inclusive.",
    )

    @model_validator(mode="after")
    def validate_station_scope(self) -> LogScope:
        if self.station_scope == StationScope.CALLSIGN and not self.station_callsigns:
            raise ValueError("station_callsigns is required for station_scope='callsign'")
        if self.station_scope != StationScope.CALLSIGN and self.station_callsigns:
            raise ValueError("station_callsigns is only valid for station_scope='callsign'")
        if self.station_scope == StationScope.PROFILE and not self.station_profile_names:
            raise ValueError("station_profile_names is required for station_scope='profile'")
        if self.station_scope != StationScope.PROFILE and self.station_profile_names:
            raise ValueError("station_profile_names is only valid for station_scope='profile'")

        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must not be later than date_to")

        return self


class SortSpec(BaseModel):
    """One semantic field used to order individual QSOs."""

    field: str = Field(
        description="Sortable semantic field returned by qlog.get_schema."
    )
    direction: SortDirection = Field(
        default=SortDirection.ASC,
        description="Sort direction for this field.",
    )


class OnePerGroup(BaseModel):
    """Keep one complete QSO for each distinct group of field values."""

    fields: list[str] = Field(
        min_length=1,
        description=(
            "Semantic fields or derived time dimensions defining each group. A list field "
            "uses its individual item when qso.aggregate also explodes that field in group_by; "
            "otherwise it uses the complete stored string."
        ),
    )
    keep: Literal["first", "last"] = Field(
        description=(
            "Keep the earliest or latest QSO by datetime; the contact ID breaks ties."
        ),
    )


class AggregateMetric(BaseModel):
    """One aggregate value calculated for every result group."""

    model_config = ConfigDict(populate_by_name=True)

    function: AggregateFunction = Field(description="Aggregate function to calculate.")
    field: str | None = Field(
        default=None,
        description=(
            "One semantic QSO field to aggregate. Omit it for count. For distinct_count, "
            "use exactly one of field or fields. Other functions require field. Use the "
            "field's aggregate_functions from qlog.get_schema."
        ),
    )
    fields: list[str] | None = Field(
        default=None,
        min_length=1,
        max_length=10,
        description=(
            "For distinct_count only, 1 to 10 scalar semantic fields forming a typed tuple. "
            "A QSO is excluded when any component is null or empty; text components compare "
            "case-insensitively. Use exactly one of field or fields."
        ),
    )
    output_name: str = Field(
        alias="as",
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
        description="Name of this metric in every returned row.",
    )
    filters: FilterGroup | None = Field(
        default=None,
        description=(
            "Optional filters applied only to this metric, in addition to the query scope "
            "and top-level filters."
        ),
    )
    explode: bool = Field(
        default=False,
        description=(
            "For distinct_count on a cardinality=many field, count normalized semantic "
            "items instead of distinct complete stored strings. It requires singular field; "
            "leave false for fields tuples, scalar fields, and every other function."
        ),
    )

    @model_validator(mode="after")
    def validate_field(self) -> AggregateMetric:
        supplied = sum((self.field is not None, self.fields is not None))
        if self.function == AggregateFunction.COUNT:
            if supplied:
                raise ValueError(
                    "count does not accept field or fields; filter the metric to count a subset"
                )
        elif self.function == AggregateFunction.DISTINCT_COUNT:
            if supplied != 1:
                raise ValueError("distinct_count requires exactly one of field or fields")
        elif self.field is None or self.fields is not None:
            raise ValueError(f"{self.function.value} requires field and does not accept fields")
        if self.explode and self.function != AggregateFunction.DISTINCT_COUNT:
            raise ValueError("explode is supported only for distinct_count")
        if self.explode and self.fields is not None:
            raise ValueError("explode is not supported with composite fields")
        return self


class AggregateSort(BaseModel):
    """One returned aggregate dimension or metric used to order groups."""

    field: str = Field(
        description="A group_by dimension or metric alias returned by this aggregation."
    )
    direction: SortDirection = Field(
        default=SortDirection.ASC,
        description="Sort direction for this aggregate result field.",
    )


class AggregateHaving(BaseModel):
    """Post-aggregation comparison that removes completed result groups."""

    field: str = Field(description="Metric alias produced by this aggregation.")
    op: HavingOperator = Field(
        default=HavingOperator.EQ,
        description="Comparison applied to the aggregate metric.",
    )
    value: Scalar = Field(
        description="Scalar threshold compared with the completed aggregate metric."
    )


class GroupBySpec(BaseModel):
    """Transform one semantic field into an aliased aggregate group dimension."""

    model_config = ConfigDict(populate_by_name=True)

    field: str = Field(
        description=(
            "Semantic QSO field to transform before grouping. Use interval for date/time, "
            "bucket_size for numbers, or explode=true for cardinality=many fields."
        )
    )
    output_name: str = Field(
        alias="as",
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
        description=(
            "Unique name of this group dimension in returned rows; it must not duplicate "
            "another group dimension or metric alias."
        ),
    )
    interval: GroupInterval | None = Field(
        default=None,
        description=(
            "Time bucket for a date or datetime field; minute and hour require datetime. "
            "Minute also requires size. Do not combine with bucket_size or explode."
        ),
    )
    size: int | None = Field(
        default=None,
        ge=1,
        le=60,
        description=(
            "Minute-bucket width from 1 through 60. Required with interval=minute and invalid "
            "with every other interval, bucket_size, or explode."
        ),
    )
    bucket_size: float | None = Field(
        default=None,
        gt=0,
        allow_inf_nan=False,
        description=(
            "Positive bucket width for an integer or number field. Do not combine with "
            "interval or explode."
        ),
    )
    explode: bool = Field(
        default=False,
        description=(
            "Set true to create one group per normalized semantic item of a "
            "cardinality=many field. Duplicate items within one QSO contribute once. Do not "
            "combine with interval or bucket_size."
        ),
    )

    @model_validator(mode="after")
    def validate_definition(self) -> GroupBySpec:
        choices = sum(
            (self.interval is not None, self.bucket_size is not None, self.explode)
        )
        if choices != 1:
            raise ValueError(
                "group_by object requires exactly one of interval, bucket_size, or explode=true"
            )
        if self.interval == GroupInterval.MINUTE and self.size is None:
            raise ValueError("interval=minute requires size")
        if self.interval != GroupInterval.MINUTE and self.size is not None:
            raise ValueError("size is valid only with interval=minute")
        return self


@dataclass(frozen=True, slots=True)
class QsoField:
    column: str
    value_type: str
    description: str
    filterable: bool = True
    sortable: bool = True
    enum_name: str | None = None
    enum_values: dict[str, str] | None = None
    sql_expression: str | None = None
    required_columns: tuple[str, ...] = ()
    cardinality: FieldCardinality = "one"
    item_type: str | None = None
    side: FieldSide | None = None
    paired_field: str | None = None

    def available(self, columns: set[str]) -> bool:
        required = self.required_columns or (self.column,)
        return all(column in columns for column in required)

    def operators(self) -> tuple[FilterOperator, ...]:
        if self.value_type == "boolean":
            return BOOLEAN_OPERATORS
        text_operators = TEXT_OPERATORS if self.value_type == "string" else ()
        list_operators = LIST_OPERATORS if self.cardinality == "many" else ()
        return COMPARISON_OPERATORS + text_operators + PRESENCE_OPERATORS + list_operators

    def aggregate_functions(self) -> tuple[AggregateFunction, ...]:
        functions = [AggregateFunction.DISTINCT_COUNT]
        if self.value_type in {"integer", "number"}:
            functions.extend(
                (
                    AggregateFunction.SUM,
                    AggregateFunction.AVG,
                    AggregateFunction.MIN,
                    AggregateFunction.MAX,
                )
            )
        elif self.value_type in {"date", "datetime"}:
            functions.extend((AggregateFunction.MIN, AggregateFunction.MAX))
        return tuple(functions)

    def base_expression(self, table_alias: str = "c") -> str:
        if self.sql_expression is not None:
            return self.sql_expression.format(table_alias=table_alias)
        return f'{table_alias}."{self.column}"'

    def select_expression(self, table_alias: str = "c") -> str:
        column = self.base_expression(table_alias)
        if self.value_type == "datetime":
            return f"strftime('%Y-%m-%dT%H:%M:%SZ', {column})"
        if self.value_type == "date":
            return f"strftime('%Y-%m-%d', {column})"
        return column

    def comparison_expression(self, table_alias: str = "c") -> str:
        column = self.base_expression(table_alias)
        if self.value_type == "datetime":
            return f"datetime({column})"
        if self.value_type == "date":
            return f"date({column})"
        if self.value_type == "string":
            return f"{column} COLLATE NOCASE"
        return column

    def comparison_placeholder(self) -> str:
        if self.value_type == "datetime":
            return "datetime(?)"
        if self.value_type == "date":
            return "date(?)"
        return "?"

    def aggregate_expression(self, table_alias: str = "c") -> str:
        column = self.base_expression(table_alias)
        nonempty = f"NULLIF(TRIM(CAST({column} AS TEXT)), '')"
        if self.value_type in {"boolean", "integer"}:
            return f"CAST({nonempty} AS INTEGER)"
        if self.value_type == "number":
            return f"CAST({nonempty} AS REAL)"
        if self.value_type == "datetime":
            return f"strftime('%Y-%m-%dT%H:%M:%SZ', {column})"
        if self.value_type == "date":
            return f"strftime('%Y-%m-%d', {column})"
        if self.value_type == "string":
            return f"{nonempty} COLLATE NOCASE"
        return nonempty


@dataclass(frozen=True, slots=True)
class _ListItem:
    value: str
    identities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ListSyntax:
    item_type: str
    delimiter: str
    parse_item: Callable[[str], list[_ListItem]]
    normalize_query: Callable[[str], str]
    matching: str
    explosion: str


def _normalize_list_text(value: str) -> str:
    return value.strip().upper()


def _raw_list_item(value: str) -> _ListItem:
    normalized = _normalize_list_text(value)
    return _ListItem(normalized, (normalized,))


def _parse_exact_list_item(value: str) -> list[_ListItem]:
    return [_raw_list_item(value)]


def _parse_pota_list_item(value: str) -> list[_ListItem]:
    parts = value.split("@")
    if len(parts) == 2 and all(part.strip() for part in parts):
        park, location = map(_normalize_list_text, parts)
        return [_ListItem(park, (park, f"{park}@{location}"))]
    return [_raw_list_item(value)]


def _normalize_pota_query(value: str) -> str:
    item = _parse_pota_list_item(value)[0]
    return item.identities[-1] if "@" in value and len(item.identities) == 2 else item.value


def _parse_credit_list_item(value: str) -> list[_ListItem]:
    parts = value.split(":")
    if len(parts) == 2 and parts[0].strip():
        media = [_normalize_list_text(part) for part in parts[1].split("&")]
        if media and all(media):
            credit = _normalize_list_text(parts[0])
            identities = (credit, *(f"{credit}:{medium}" for medium in media))
            return [_ListItem(credit, identities)]
    return [_raw_list_item(value)]


def _normalize_credit_query(value: str) -> str:
    item = _parse_credit_list_item(value)[0]
    if value.count(":") == 1 and "&" not in value and len(item.identities) > 1:
        return item.identities[1]
    if ":" not in value:
        return item.value
    return _normalize_list_text(value)


def _parse_county_list_item(value: str) -> list[_ListItem]:
    parts = value.split(",")
    if len(parts) == 2 and all(part.strip() for part in parts):
        normalized = ",".join(_normalize_list_text(part) for part in parts)
        return [_ListItem(normalized, (normalized,))]
    return [_raw_list_item(value)]


def _parse_alternate_county_list_item(value: str) -> list[_ListItem]:
    parts = value.split(":")
    if len(parts) == 2 and parts[0].strip():
        localities = [_normalize_list_text(part) for part in parts[1].split("/")]
        if localities and all(localities):
            enumeration = _normalize_list_text(parts[0])
            return [
                _ListItem(f"{enumeration}:{locality}", (f"{enumeration}:{locality}",))
                for locality in localities
            ]
    return [_raw_list_item(value)]


def _normalize_single_parsed_item(
    value: str, parser: Callable[[str], list[_ListItem]]
) -> str:
    items = parser(value)
    return items[0].value if len(items) == 1 else _normalize_list_text(value)


def _normalize_county_query(value: str) -> str:
    return _normalize_single_parsed_item(value, _parse_county_list_item)


def _normalize_alternate_county_query(value: str) -> str:
    return _normalize_single_parsed_item(value, _parse_alternate_county_list_item)


def _parse_list(field_name: str, stored_value: object) -> list[_ListItem]:
    syntax = _LIST_SYNTAXES[field_name]
    if stored_value is None:
        return []

    items: list[_ListItem] = []
    for chunk in str(stored_value).split(syntax.delimiter):
        if chunk.strip():
            items.extend(syntax.parse_item(chunk))
    return items


def _list_has(field_name: object, stored_value: object, requested_value: object) -> int:
    if not isinstance(field_name, str) or not isinstance(requested_value, str):
        return 0
    syntax = _LIST_SYNTAXES.get(field_name)
    if syntax is None or not requested_value.strip():
        return 0
    requested = syntax.normalize_query(requested_value)
    return int(
        any(requested in item.identities for item in _parse_list(field_name, stored_value))
    )


def _list_values(field_name: object, stored_value: object) -> str:
    if not isinstance(field_name, str) or field_name not in _LIST_SYNTAXES:
        return ""
    values: list[str] = []
    seen: set[str] = set()
    for item in _parse_list(field_name, stored_value):
        if item.value and item.value not in seen:
            seen.add(item.value)
            values.append(item.value)
    # The recursive SQL splitter uses SQLite char(31) for this internal separator.
    return "\x1f".join(values)


def _list_distinct_count(field_name: object, stored_values: object) -> int:
    if not isinstance(field_name, str) or field_name not in _LIST_SYNTAXES:
        return 0
    return len({item.value for item in _parse_list(field_name, stored_values) if item.value})


def _typed_tuple(*values: object) -> str:
    """Encode SQLite scalar values without losing their types or component boundaries."""
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


def _field(
    column: str,
    value_type: str,
    description: str,
    *,
    enum_name: str | None = None,
    enum_values: dict[str, str] | None = None,
    sql_expression: str | None = None,
    required_columns: tuple[str, ...] = (),
) -> QsoField:
    return QsoField(
        column=column,
        value_type=value_type,
        description=description,
        enum_name=enum_name,
        enum_values=enum_values,
        sql_expression=sql_expression,
        required_columns=required_columns,
    )


def _grid4_field(column: str, description: str) -> QsoField:
    normalized = f'UPPER(TRIM(CAST({{table_alias}}."{column}" AS TEXT)))'
    valid = " OR ".join(
        f"{normalized} GLOB '{pattern}'"
        for pattern in (
            "[A-R][A-R][0-9][0-9]",
            "[A-R][A-R][0-9][0-9][A-X][A-X]",
            "[A-R][A-R][0-9][0-9][A-X][A-X][0-9][0-9]",
        )
    )
    return _field(
        column,
        "string",
        description,
        sql_expression=f"CASE WHEN {valid} THEN SUBSTR({normalized}, 1, 4) END",
        required_columns=(column,),
    )


def _autovalue_field(
    column: str,
    value_type: str,
    description: str,
    *,
    enum_name: str | None = None,
    enum_values: dict[str, str] | None = None,
) -> QsoField:
    """Define a field from the optional one-to-one contacts_autovalue row."""
    qualified_column = f"contacts_autovalue.{column}"
    return _field(
        qualified_column,
        value_type,
        description,
        enum_name=enum_name,
        enum_values=enum_values,
        sql_expression=f'a."{column}"',
        required_columns=("contacts_autovalue.contactid", qualified_column),
    )


def _numeric_rst_field(column: str, description: str) -> QsoField:
    """Expose an ADIF RST value only when its complete text is an integer."""
    value = f'TRIM(CAST({{table_alias}}."{column}" AS TEXT))'
    unsigned = f"({value} GLOB '[0-9]*' AND {value} NOT GLOB '*[^0-9]*')"
    signed = (
        f"(({value} GLOB '-[0-9]*' OR {value} GLOB '+[0-9]*') "
        f"AND SUBSTR({value}, 2) NOT GLOB '*[^0-9]*')"
    )
    return _field(
        column,
        "integer",
        description,
        sql_expression=f"CASE WHEN {unsigned} OR {signed} THEN CAST({value} AS INTEGER) END",
    )


def _upload_status_field(
    column: str,
    service: str,
    *,
    autovalue: bool = False,
) -> QsoField:
    factory = _autovalue_field if autovalue else _field
    return factory(
        column,
        "string",
        f"ADIF QSO Upload Status for {service}",
        enum_name="ADIF QSO Upload Status",
        enum_values=QSO_UPLOAD_STATUS_VALUES,
    )


QSO_FIELDS: dict[str, QsoField] = {
    "id": _field("id", "integer", "Internal QLog contact identifier"),
    "datetime": _field("start_time", "datetime", "UTC QSO start time corresponding to ADIF QSO_DATE + TIME_ON"),
    "datetime_off": _field("end_time", "datetime", "UTC QSO end time corresponding to ADIF QSO_DATE_OFF + TIME_OFF"),
    "duration_seconds": _field(
        "end_time",
        "integer",
        "Elapsed QSO duration in seconds, calculated as UTC end time minus UTC start time; NULL when either timestamp is missing or invalid, or when the end precedes the start",
        sql_expression=(
            "CASE WHEN julianday({table_alias}.\"end_time\") >= "
            "julianday({table_alias}.\"start_time\") THEN "
            "CAST(ROUND((julianday({table_alias}.\"end_time\") - "
            "julianday({table_alias}.\"start_time\")) * 86400) AS INTEGER) END"
        ),
        required_columns=("start_time", "end_time"),
    ),
    "callsign": _field("callsign", "string", "ADIF CALL: callsign of the station contacted or heard; this is the remote station, not the logging station or operator"),
    "rst_sent": _field("rst_sent", "string", "Signal report sent by the logging station to the contacted station"),
    "rst_received": _field("rst_rcvd", "string", "Signal report received by the logging station from the contacted station"),
    "rst_sent_numeric": _numeric_rst_field(
        "rst_sent",
        "Integer form of ADIF RST_SENT for numeric filtering and aggregation; NULL when the complete report is not an integer, for example 5NN. Compare only QSOs using compatible report conventions: CW/phone reports such as 599 or 59 and digital signal reports such as -12 use different scales",
    ),
    "rst_received_numeric": _numeric_rst_field(
        "rst_rcvd",
        "Integer form of ADIF RST_RCVD for numeric filtering and aggregation; NULL when the complete report is not an integer, for example 5NN. Compare only QSOs using compatible report conventions: CW/phone reports such as 599 or 59 and digital signal reports such as -12 use different scales",
    ),
    "frequency": _field("freq", "number", "ADIF FREQ: exact logging station transmit frequency in MHz; use this for a specific frequency such as 14.145, and use band for a whole band such as 20m or an approximate reference such as 14 MHz; a band-only QSO has no exact frequency"),
    "frequency_rx": _field("freq_rx", "number", "ADIF FREQ_RX: exact logging station receive frequency in MHz for split or cross-band QSOs; a band-only QSO has no exact receive frequency"),
    "frequency_offset_khz": _field(
        "freq_rx",
        "number",
        "Signed receive-minus-transmit frequency offset in kHz, calculated as (ADIF FREQ_RX - ADIF FREQ) * 1000; positive means the logging station received above its transmit frequency and negative means below. NULL unless both exact frequencies are logged",
        sql_expression=(
            "ROUND((CAST(NULLIF(TRIM(CAST({table_alias}.\"freq_rx\" AS TEXT)), '') "
            "AS REAL) - CAST(NULLIF(TRIM(CAST({table_alias}.\"freq\" AS TEXT)), '') "
            "AS REAL)) * 1000, 6)"
        ),
        required_columns=("freq", "freq_rx"),
    ),
    "band": _field("band", "string", "Canonical transmit band such as 20m; when ADIF BAND is empty and the QLog bands table is available, derive it from FREQ so band filters and groups include frequency-only QSOs"),
    "band_rx": _field("band_rx", "string", "Canonical receive band for split or cross-band QSOs; when ADIF BAND_RX is empty and the QLog bands table is available, derive it from FREQ_RX"),
    "is_split": _field(
        "freq_rx",
        "boolean",
        "True when the logged receive and transmit frequencies differ by at least 1 Hz, or when the resolved receive and transmit bands differ; otherwise false. Resolved bands use stored BAND/BAND_RX with frequency-based fallback. This describes information recorded in the QSO, not the radio's unlogged split state",
        required_columns=("freq", "freq_rx", "band", "band_rx"),
    ),
    "is_cross_band": _field(
        "band_rx",
        "boolean",
        "True when the resolved receive and transmit bands are both known and differ; otherwise false. Resolved bands use stored BAND/BAND_RX with frequency-based fallback, so frequency-only QSOs are recognized when QLog's bands table is available",
        required_columns=("band", "band_rx"),
    ),
    "mode": _field(
        "mode",
        "string",
        "Operator-facing operating mode: returns ADIF SUBMODE when present, otherwise ADIF MODE; use this field for values such as FT8, FT4, USB or CW",
        sql_expression='COALESCE(NULLIF({table_alias}."submode", \'\'), {table_alias}."mode")',
        required_columns=("mode", "submode"),
    ),
    "adif_mode": _field("mode", "string", "ADIF MODE parent category stored in the QSO; unlike mode, this does not substitute SUBMODE"),
    "submode": _field("submode", "string", "ADIF SUBMODE; its valid meaning is tied to the parent ADIF MODE"),
    "name": _field("name", "string", "Contact name"),
    "qth": _field("qth", "string", "Contact location name"),
    "grid": _field("gridsquare", "string", "ADIF GRIDSQUARE: contacted station Maidenhead locator up to 8 characters; combine with grid_ext for 10- or 12-character locators"),
    "grid4": _grid4_field(
        "gridsquare",
        "Uppercase four-character Maidenhead square derived from the contacted station grid; NULL unless GRIDSQUARE is a valid 4-, 6-, or 8-character locator",
    ),
    "dxcc": _field("dxcc", "integer", "ADIF DXCC numeric entity code of the contacted station; prefer this over country text when filtering by DXCC entity"),
    "country": _field("country", "string", "Contacted station DXCC entity name as stored in the log; use dxcc for stable entity-code filtering"),
    "continent": _field("cont", "string", "Contact continent code"),
    "cq_zone": _field("cqz", "integer", "Contact CQ zone"),
    "itu_zone": _field("ituz", "integer", "Contact ITU zone"),
    "prefix": _field("pfx", "string", "Contact WPX prefix"),
    "state": _field("state", "string", "ADIF STATE primary administrative subdivision of the contacted station; valid values depend on dxcc"),
    "county": _field("cnty", "string", "ADIF CNTY secondary administrative subdivision of the contacted station; interpretation depends on state and therefore dxcc"),
    "iota": _field("iota", "string", "ADIF IOTA contacted-station island-group reference in CC-NNN form, for example EU-001; this is not a specific island ID"),
    "pota_ref": _field("pota_ref", "string", "ADIF POTA_REF: comma-delimited contacted-station park references; an item may include an @ location suffix, for example K-4562@US-CA"),
    "sota_ref": _field("sota_ref", "string", "ADIF SOTA_REF: contacted-station summit reference"),
    "wwff_ref": _field("wwff_ref", "string", "ADIF WWFF_REF: contacted-station WWFF reference"),
    "sig": _field("sig", "string", "ADIF SIG: contacted-station special-interest group name; interpret sig_info in this group's context"),
    "sig_info": _field("sig_info", "string", "ADIF SIG_INFO: contacted-station reference whose syntax and meaning are defined by sig"),
    "qsl_received": _field("qsl_rcvd", "string", "ADIF QSL_RCVD status for paper QSL; this is an ADIF QSL Rcvd enumeration, not a boolean"),
    "qsl_received_date": _field("qsl_rdate", "date", "ADIF QSLRDATE: UTC date on which paper QSL confirmation was received"),
    "qsl_sent": _field("qsl_sent", "string", "ADIF QSL_SENT status for paper QSL; this is an ADIF QSL Sent enumeration, not a boolean"),
    "qsl_sent_date": _field("qsl_sdate", "date", "ADIF QSLSDATE: UTC date on which paper QSL confirmation was sent"),
    "lotw_received": _field("lotw_qsl_rcvd", "string", "ADIF LOTW_QSL_RCVD status; uses the ADIF QSL Rcvd enumeration, not a boolean"),
    "lotw_received_date": _field("lotw_qslrdate", "date", "LoTW received date"),
    "lotw_sent": _field("lotw_qsl_sent", "string", "ADIF LOTW_QSL_SENT status; uses the ADIF QSL Sent enumeration, not a boolean"),
    "lotw_sent_date": _field("lotw_qslsdate", "date", "LoTW sent date"),
    "eqsl_received": _field("eqsl_qsl_rcvd", "string", "ADIF EQSL_QSL_RCVD status; uses the ADIF QSL Rcvd enumeration, not a boolean"),
    "eqsl_received_date": _field("eqsl_qslrdate", "date", "eQSL received date"),
    "eqsl_sent": _field("eqsl_qsl_sent", "string", "ADIF EQSL_QSL_SENT status; uses the ADIF QSL Sent enumeration, not a boolean"),
    "eqsl_sent_date": _field("eqsl_qslsdate", "date", "eQSL sent date"),
    "dcl_received": _field("dcl_qsl_rcvd", "string", "ADIF DCL_QSL_RCVD status; uses the ADIF QSL Rcvd enumeration, not a boolean"),
    "dcl_received_date": _field("dcl_qslrdate", "date", "DCL received date"),
    "dcl_sent": _field("dcl_qsl_sent", "string", "ADIF DCL_QSL_SENT status; uses the ADIF QSL Sent enumeration, not a boolean"),
    "dcl_sent_date": _field("dcl_qslsdate", "date", "DCL sent date"),
    "tx_power": _field("tx_pwr", "number", "ADIF TX_PWR: logging station transmitter power in Watts"),
    "rx_power": _field("rx_pwr", "number", "ADIF RX_PWR: contacted station transmitter power in Watts"),
    "distance": _field("distance", "number", "ADIF DISTANCE: distance between logging and contacted stations, in kilometers"),
    "comment": _field("comment", "string", "QSO comment"),
    "notes": _field("notes", "string", "QSO notes"),
    "operator": _field("operator", "string", "ADIF OPERATOR: callsign of the person operating the logging station; do not confuse with station_callsign or contacted_operator"),
    "station_callsign": _field("station_callsign", "string", "ADIF STATION_CALLSIGN: callsign used over the air by the logging station for this QSO; this defines local station identity"),
    "my_grid": _field("my_gridsquare", "string", "ADIF MY_GRIDSQUARE: logging station Maidenhead locator up to 8 characters; combine with my_grid_ext for 10- or 12-character locators"),
    "my_grid4": _grid4_field(
        "my_gridsquare",
        "Uppercase four-character Maidenhead square derived from the logging station grid; NULL unless MY_GRIDSQUARE is a valid 4-, 6-, or 8-character locator",
    ),
    "my_dxcc": _field("my_dxcc", "integer", "ADIF MY_DXCC numeric DXCC entity code of the logging station"),
    "my_country": _field("my_country", "string", "Local station country"),
    "my_cq_zone": _field("my_cq_zone", "integer", "Local station CQ zone"),
    "my_itu_zone": _field("my_itu_zone", "integer", "Local station ITU zone"),
    "my_iota": _field("my_iota", "string", "ADIF MY_IOTA: logging-station island-group reference in CC-NNN form"),
    "my_pota_ref": _field("my_pota_ref", "string", "ADIF MY_POTA_REF: comma-delimited logging-station park references; an item may include an @ location suffix"),
    "my_sota_ref": _field("my_sota_ref", "string", "ADIF MY_SOTA_REF: logging-station summit reference"),
    "my_wwff_ref": _field("my_wwff_ref", "string", "ADIF MY_WWFF_REF: logging-station WWFF reference"),
    "my_sig": _field("my_sig", "string", "ADIF MY_SIG: logging-station special-interest group name; interpret my_sig_info in this group's context"),
    "my_sig_info": _field("my_sig_info", "string", "ADIF MY_SIG_INFO: logging-station reference whose syntax and meaning are defined by my_sig"),
    "propagation_mode": _field("prop_mode", "string", "ADIF PROP_MODE enumeration describing the recorded propagation mechanism, for example SAT, EME, ES or F2; an empty value is unknown and does not prove direct propagation"),
    "satellite_name": _field("sat_name", "string", "ADIF SAT_NAME: satellite name used for the QSO; normally meaningful when propagation_mode is SAT"),
    "satellite_mode": _field("sat_mode", "string", "ADIF SAT_MODE: satellite transponder mode used for the QSO; normally meaningful for satellite QSOs"),
    "contest_id": _field("contest_id", "string", "ADIF CONTEST_ID enumeration identifying the contest; contest exchange fields should be interpreted in this contest context"),
    "serial_sent": _field("stx", "integer", "ADIF STX: numeric contest serial number transmitted by the logging station"),
    "serial_received": _field("srx", "integer", "ADIF SRX: numeric contest serial number received from the contacted station"),
    "exchange_sent": _field("stx_string", "string", "ADIF STX_STRING: non-numeric or composite contest exchange transmitted by the logging station"),
    "exchange_received": _field("srx_string", "string", "ADIF SRX_STRING: non-numeric or composite contest exchange received from the contacted station"),
    # Additional ADIF fields stored in the QLog contacts table.
    "address": _field("address", "string", "Contacted station's mailing address"),
    "address_intl": _field("address_intl", "string", "Contacted station's internationalized mailing address"),
    "age": _field("age", "integer", "Contacted operator's age in years"),
    "a_index": _field("a_index", "integer", "Geomagnetic A index at the time of the QSO"),
    "antenna_azimuth": _field("ant_az", "number", "ADIF ANT_AZ: logging station antenna azimuth in degrees, 0=true north and increasing clockwise"),
    "antenna_elevation": _field("ant_el", "number", "ADIF ANT_EL: logging station antenna elevation in degrees, 0=horizon, positive upward, nominal range -90..90"),
    "antenna_path": _field("ant_path", "string", "ADIF ANT_PATH enumeration: G=grayline, O=other, S=short path, L=long path"),
    "arrl_section": _field("arrl_sect", "string", "Contacted station's ARRL section"),
    "award_submitted": _field("award_submitted", "string", "ADIF AWARD_SUBMITTED: comma-delimited SponsoredAwardList submitted to a sponsor; distinct from QSL or upload status"),
    "award_granted": _field("award_granted", "string", "ADIF AWARD_GRANTED: comma-delimited SponsoredAwardList granted by a sponsor; distinct from QSL or upload status"),
    "contest_check": _field("check", "string", "ADIF CHECK: contest check value received from the contacted station; meaning depends on contest_id"),
    "contest_class": _field("class", "string", "ADIF CLASS: contest class received from the contacted station; meaning depends on contest_id"),
    "clublog_upload_date": _field("clublog_qso_upload_date", "date", "Date the QSO was last uploaded to Club Log"),
    "clublog_upload_status": _upload_status_field(
        "clublog_qso_upload_status", "Club Log"
    ),
    "comment_intl": _field("comment_intl", "string", "Internationalized QSO comment"),
    "contacted_operator": _field("contacted_op", "string", "ADIF CONTACTED_OP: callsign of the person operating the contacted station when different from or more specific than CALL"),
    "country_intl": _field("country_intl", "string", "Internationalized contacted station DXCC entity name"),
    "credit_submitted": _field("credit_submitted", "string", "ADIF CREDIT_SUBMITTED: comma-delimited CreditList sought for this QSO; an item may add QSL media after ':' and join multiple media with '&'"),
    "credit_granted": _field("credit_granted", "string", "ADIF CREDIT_GRANTED: comma-delimited CreditList granted for this QSO; an item may add QSL media after ':' and join multiple media with '&'; distinct from received-QSL and upload status"),
    "darc_dok": _field("darc_dok", "string", "Contacted station's DARC DOK"),
    "email": _field("email", "string", "Contacted station's email address"),
    "eq_call": _field("eq_call", "string", "ADIF EQ_CALL: callsign of the owner of the contacted station"),
    "fists": _field("fists", "integer", "Contacted station's FISTS member number"),
    "fists_cc": _field("fists_cc", "integer", "Contacted station's FISTS Century Certificate number"),
    "force_init": _field("force_init", "string", "ADIF FORCE_INIT boolean (Y/N): force this QSO to count as a new EME initial contact"),
    "guest_operator": _field("guest_op", "string", "ADIF GUEST_OP callsign; deprecated/import-only legacy field, prefer operator for current operator identity"),
    "hrdlog_upload_date": _field("hrdlog_qso_upload_date", "date", "Date the QSO was last uploaded to HRDLog.net"),
    "hrdlog_upload_status": _upload_status_field(
        "hrdlog_qso_upload_status", "HRDLog.net"
    ),
    "iota_island_id": _field("iota_island_id", "integer", "ADIF IOTA_ISLAND_ID: numeric identifier of the contacted station's specific island; iota identifies the broader island group"),
    "k_index": _field("k_index", "integer", "Geomagnetic K index at the time of the QSO"),
    "latitude": _field("lat", "string", "ADIF LAT location value for the contacted station; stored in ADIF Location format, not decimal-degrees numeric form"),
    "longitude": _field("lon", "string", "ADIF LON location value for the contacted station; stored in ADIF Location format, not decimal-degrees numeric form"),
    "max_bursts": _field("max_bursts", "number", "Maximum meteor-scatter burst length heard, in seconds"),
    "meteor_shower": _field("ms_shower", "string", "Meteor shower in progress during a meteor-scatter QSO"),
    "my_antenna": _field("my_antenna", "string", "Logging station's antenna description"),
    "my_antenna_intl": _field("my_antenna_intl", "string", "Internationalized logging station antenna description"),
    "my_city": _field("my_city", "string", "Logging station's city"),
    "my_city_intl": _field("my_city_intl", "string", "Internationalized logging station city"),
    "my_county": _field("my_cnty", "string", "ADIF MY_CNTY secondary administrative subdivision of the logging station; interpretation depends on my_state and my_dxcc"),
    "my_country_intl": _field("my_country_intl", "string", "Internationalized logging station DXCC entity name"),
    "my_fists": _field("my_fists", "integer", "Logging station operator's FISTS member number"),
    "my_iota_island_id": _field("my_iota_island_id", "integer", "ADIF MY_IOTA_ISLAND_ID: numeric identifier of the logging station's specific island; my_iota identifies the broader island group"),
    "my_latitude": _field("my_lat", "string", "ADIF MY_LAT location value for the logging station; stored in ADIF Location format, not decimal-degrees numeric form"),
    "my_longitude": _field("my_lon", "string", "ADIF MY_LON location value for the logging station; stored in ADIF Location format, not decimal-degrees numeric form"),
    "my_name": _field("my_name", "string", "Logging station operator's name"),
    "my_name_intl": _field("my_name_intl", "string", "Internationalized logging station operator name"),
    "my_postal_code": _field("my_postal_code", "string", "Logging station's postal code"),
    "my_postal_code_intl": _field("my_postal_code_intl", "string", "Internationalized logging station postal code"),
    "my_rig": _field("my_rig", "string", "Logging station's radio equipment description"),
    "my_rig_intl": _field("my_rig_intl", "string", "Internationalized logging station radio equipment description"),
    "my_sig_intl": _field("my_sig_intl", "string", "Internationalized logging station special-interest group name"),
    "my_sig_info_intl": _field("my_sig_info_intl", "string", "Internationalized logging station special-interest group information"),
    "my_state": _field("my_state", "string", "ADIF MY_STATE primary administrative subdivision of the logging station; valid values depend on my_dxcc"),
    "my_street": _field("my_street", "string", "Logging station's street address"),
    "my_street_intl": _field("my_street_intl", "string", "Internationalized logging station street address"),
    "my_usaca_counties": _field("my_usaca_counties", "string", "ADIF MY_USACA_COUNTIES: colon-delimited logging-station county list; each item is a STATE,County pair, for example MA,Franklin:MA,Hampshire"),
    "my_vucc_grids": _field("my_vucc_grids", "string", "ADIF MY_VUCC_GRIDS: comma-delimited list of Maidenhead grid squares credited to the logging station for the QSO"),
    "name_intl": _field("name_intl", "string", "Internationalized contacted operator name"),
    "notes_intl": _field("notes_intl", "string", "Internationalized QSO notes"),
    "nr_bursts": _field("nr_bursts", "integer", "Number of meteor-scatter bursts heard by the logging station"),
    "nr_pings": _field("nr_pings", "integer", "Number of meteor-scatter pings heard by the logging station"),
    "owner_callsign": _field("owner_callsign", "string", "ADIF OWNER_CALLSIGN: callsign of the owner of the logging station equipment; distinct from operator and station_callsign"),
    "contest_precedence": _field("precedence", "string", "ADIF PRECEDENCE contest exchange value, primarily used by ARRL Sweepstakes; interpret in contest_id context"),
    "public_key": _field("public_key", "string", "Public encryption key"),
    "qrzcom_upload_date": _field("qrzcom_qso_upload_date", "date", "Date the QSO was last uploaded to QRZ.com"),
    "qrzcom_upload_status": _upload_status_field(
        "qrzcom_qso_upload_status", "QRZ.com"
    ),
    "qsl_message": _field("qslmsg", "string", "Message for the contacted operator to be included on a QSL"),
    "qsl_message_intl": _field("qslmsg_intl", "string", "Internationalized message for the contacted operator to be included on a QSL"),
    "qsl_received_via": _field("qsl_rcvd_via", "string", "ADIF QSL_RCVD_VIA enumeration describing how the QSL was received by the logging station"),
    "qsl_sent_via": _field("qsl_sent_via", "string", "ADIF QSL_SENT_VIA enumeration describing how the QSL was sent by the logging station"),
    "qsl_via": _field("qsl_via", "string", "QSL routing information, such as a QSL manager callsign"),
    "qso_complete": _field("qso_complete", "string", "ADIF QSO_COMPLETE enumeration describing whether the QSO was completed or why it was incomplete; not a boolean"),
    "qso_random": _field("qso_random", "string", "ADIF QSO_RANDOM boolean (Y/N): whether the QSO frequency was selected randomly rather than by schedule"),
    "qth_intl": _field("qth_intl", "string", "Internationalized contacted station location name"),
    "region": _field("region", "string", "ADIF REGION enumeration value for award/contest region context; do not interpret as arbitrary free-form geographic region"),
    "rig": _field("rig", "string", "Contacted station's radio equipment description"),
    "rig_intl": _field("rig_intl", "string", "Internationalized contacted station radio equipment description"),
    "solar_flux_index": _field("sfi", "integer", "Solar flux index at the time of the QSO"),
    "sig_intl": _field("sig_intl", "string", "Internationalized contacted station special-interest group name"),
    "sig_info_intl": _field("sig_info_intl", "string", "Internationalized contacted station special-interest group information"),
    "silent_key": _field("silent_key", "string", "ADIF SILENT_KEY boolean (Y/N): whether the contacted operator is known to be deceased"),
    "skcc": _field("skcc", "string", "Contacted station's SKCC member number"),
    "swl": _field("swl", "string", "ADIF SWL boolean (Y/N): whether this record represents a short-wave-listener report rather than a normal two-way QSO"),
    "ten_ten": _field("ten_ten", "integer", "Contacted station's Ten-Ten member number"),
    "uksmg": _field("uksmg", "integer", "Contacted station's UKSMG member number"),
    "usaca_counties": _field("usaca_counties", "string", "ADIF USACA_COUNTIES: colon-delimited contacted-station county list; each item is a STATE,County pair, for example MA,Franklin:MA,Hampshire"),
    "ve_province": _field("ve_prov", "string", "Contacted station's Canadian province; deprecated ADIF field"),
    "vucc_grids": _field("vucc_grids", "string", "ADIF VUCC_GRIDS: comma-delimited list of contacted-station Maidenhead grid squares credited for the QSO"),
    "web": _field("web", "string", "Contacted station's web URL"),
    "my_arrl_section": _field("my_arrl_sect", "string", "Logging station's ARRL section"),
    "altitude": _field("altitude", "number", "Contacted station's altitude above mean sea level in meters"),
    "grid_ext": _field("gridsquare_ext", "string", "ADIF GRIDSQUARE_EXT: characters 9-10 for a 10-character locator or 9-12 for a 12-character locator; append to grid"),
    "hamlogeu_upload_date": _field("hamlogeu_qso_upload_date", "date", "Date the QSO was last uploaded to HAMLOG.online/HAMLOG.eu"),
    "hamlogeu_upload_status": _upload_status_field(
        "hamlogeu_qso_upload_status", "HAMLOG.online/HAMLOG.eu"
    ),
    "hamqth_upload_date": _field("hamqth_qso_upload_date", "date", "Date the QSO was last uploaded to HamQTH"),
    "hamqth_upload_status": _upload_status_field(
        "hamqth_qso_upload_status", "HamQTH"
    ),
    "my_altitude": _field("my_altitude", "number", "Logging station's altitude above mean sea level in meters"),
    "my_grid_ext": _field("my_gridsquare_ext", "string", "ADIF MY_GRIDSQUARE_EXT: characters 9-10 for a 10-character locator or 9-12 for a 12-character locator; append to my_grid"),
    "county_alt": _field("cnty_alt", "string", "ADIF CNTY_ALT: semicolon-delimited contacted-station alternate subdivisions in enumeration-name:code form; '/' separates multiple localities within a code"),
    "morse_key_info": _field("morse_key_info", "string", "Information about the contacted station operator's Morse key"),
    "morse_key_type": _field("morse_key_type", "string", "ADIF MORSE_KEY_TYPE enumeration code identifying the type of Morse key used by the contacted station operator"),
    "my_county_alt": _field("my_cnty_alt", "string", "ADIF MY_CNTY_ALT: semicolon-delimited logging-station alternate subdivisions in enumeration-name:code form; '/' separates multiple localities within a code"),
    "my_darc_dok": _field("my_darc_dok", "string", "Logging station's DARC DOK"),
    "my_morse_key_info": _field("my_morse_key_info", "string", "Information about the logging station operator's Morse key"),
    "my_morse_key_type": _field("my_morse_key_type", "string", "ADIF MY_MORSE_KEY_TYPE enumeration code identifying the type of Morse key used by the logging station operator"),
    "qrzcom_download_date": _field("qrzcom_qso_download_date", "date", "Date the QSO was downloaded from QRZ.com"),
    "qrzcom_download_status": _field("qrzcom_qso_download_status", "string", "ADIF QRZCOM_QSO_DOWNLOAD_STATUS enumeration; represents QRZ.com download state, not a boolean"),
    "qsl_message_received": _field("qslmsg_rcvd", "string", "Message addressed to the logging operator received on a paper or electronic QSL"),
    "eqsl_authenticity_guaranteed": _field("eqsl_ag", "string", "ADIF EQSL_AG enumeration indicating eQSL Authenticity Guaranteed status; use the stored ADIF code"),
    # Derived and integration values stored one-to-one with contacts.
    "base_callsign": _autovalue_field(
        "base_callsign",
        "string",
        "Base contacted callsign computed by QLog, without portable prefixes or suffixes",
    ),
    "wavelog_upload_status": _upload_status_field(
        "wavelog_qso_upload_status", "Wavelog", autovalue=True
    ),
    "wavelog_upload_date": _autovalue_field(
        "wavelog_qso_upload_date",
        "date",
        "Date on which the QSO was last uploaded to Wavelog",
    ),
    "extra_fields": _field("fields", "object", "Additional ADIF fields stored as JSON"),
}

_POTA_LIST = _ListSyntax(
    "pota_reference",
    ",",
    _parse_pota_list_item,
    _normalize_pota_query,
    "A park reference without @location matches that park with any location; a reference "
    "with @location requires the same location.",
    "Returns the park reference without its optional @location suffix.",
)
_GRID_LIST = _ListSyntax(
    "maidenhead_grid",
    ",",
    _parse_exact_list_item,
    _normalize_list_text,
    "Matches one complete Maidenhead grid item.",
    "Returns each complete Maidenhead grid item.",
)
_COUNTY_LIST = _ListSyntax(
    "secondary_subdivision",
    ":",
    _parse_county_list_item,
    _normalize_county_query,
    "Matches one complete STATE,County pair; the comma is part of the item.",
    "Returns each complete STATE,County pair.",
)
_ALTERNATE_COUNTY_LIST = _ListSyntax(
    "alternate_secondary_subdivision",
    ";",
    _parse_alternate_county_list_item,
    _normalize_alternate_county_query,
    "Matches one enumeration-name:locality identity; slash-separated localities in a stored "
    "entry are separate identities with the same enumeration name.",
    "Returns one enumeration-name:locality value for each slash-separated locality.",
)
_CREDIT_LIST = _ListSyntax(
    "credit",
    ",",
    _parse_credit_list_item,
    _normalize_credit_query,
    "A bare CREDIT matches that credit with any recorded media; CREDIT:MEDIUM requires one "
    "specific medium among the stored ampersand-separated media. To require multiple media, "
    "use has_all with one CREDIT:MEDIUM value per medium.",
    "Returns the credit name without its optional media.",
)
_AWARD_LIST = _ListSyntax(
    "sponsored_award",
    ",",
    _parse_exact_list_item,
    _normalize_list_text,
    "Matches one complete sponsored-award item.",
    "Returns each complete sponsored-award item.",
)

# Add a field here only when one stored ADIF value contains multiple semantic items.
# The assigned syntax owns its delimiter, matching, and exploded value. A structured
# scalar such as an IOTA reference or callsign with '/' does not belong here.
_LIST_SYNTAXES = {
    "pota_ref": _POTA_LIST,
    "my_pota_ref": _POTA_LIST,
    "vucc_grids": _GRID_LIST,
    "my_vucc_grids": _GRID_LIST,
    "usaca_counties": _COUNTY_LIST,
    "my_usaca_counties": _COUNTY_LIST,
    "county_alt": _ALTERNATE_COUNTY_LIST,
    "my_county_alt": _ALTERNATE_COUNTY_LIST,
    "credit_submitted": _CREDIT_LIST,
    "credit_granted": _CREDIT_LIST,
    "award_submitted": _AWARD_LIST,
    "award_granted": _AWARD_LIST,
}

# Add a pair only when both fields describe the same fact from opposite sides of the
# contact: the contacted station and the logging station. Related fields with different
# meanings, such as callsign and operator, are not a contacted/logging pair.
_CONTACTED_LOGGING_FIELD_PAIRS = (
    ("callsign", "station_callsign"),
    ("grid", "my_grid"),
    ("grid4", "my_grid4"),
    ("grid_ext", "my_grid_ext"),
    ("dxcc", "my_dxcc"),
    ("country", "my_country"),
    ("cq_zone", "my_cq_zone"),
    ("itu_zone", "my_itu_zone"),
    ("state", "my_state"),
    ("county", "my_county"),
    ("county_alt", "my_county_alt"),
    ("iota", "my_iota"),
    ("iota_island_id", "my_iota_island_id"),
    ("pota_ref", "my_pota_ref"),
    ("sota_ref", "my_sota_ref"),
    ("wwff_ref", "my_wwff_ref"),
    ("sig", "my_sig"),
    ("sig_info", "my_sig_info"),
    ("usaca_counties", "my_usaca_counties"),
    ("vucc_grids", "my_vucc_grids"),
)

# These fields describe award-credit processing attached to the QSO record as a whole.
# They describe neither the contacted station nor the logging station and have no
# opposite-side counterpart. Their schema side is therefore "qso" and paired_field is
# intentionally absent.
_QSO_SIDE_FIELDS = (
    "credit_submitted",
    "credit_granted",
    "award_submitted",
    "award_granted",
)

for name, syntax in _LIST_SYNTAXES.items():
    QSO_FIELDS[name] = replace(
        QSO_FIELDS[name], cardinality="many", item_type=syntax.item_type
    )

for contacted_name, logging_name in _CONTACTED_LOGGING_FIELD_PAIRS:
    QSO_FIELDS[contacted_name] = replace(
        QSO_FIELDS[contacted_name], side="contacted", paired_field=logging_name
    )
    QSO_FIELDS[logging_name] = replace(
        QSO_FIELDS[logging_name], side="logging", paired_field=contacted_name
    )

QSO_FIELDS["operator"] = replace(
    QSO_FIELDS["operator"], side="operator", paired_field="contacted_operator"
)
QSO_FIELDS["contacted_operator"] = replace(
    QSO_FIELDS["contacted_operator"], side="contacted", paired_field="operator"
)

for name in _QSO_SIDE_FIELDS:
    QSO_FIELDS[name] = replace(QSO_FIELDS[name], side="qso")

DERIVED_GROUP_BY = {
    "year": {
        "type": "string",
        "description": "UTC QSO year in YYYY format",
        "expression": "strftime('%Y', c.\"start_time\")",
    },
    "month": {
        "type": "string",
        "description": "UTC QSO calendar month in YYYY-MM format",
        "expression": "strftime('%Y-%m', c.\"start_time\")",
    },
    "day": {
        "type": "string",
        "description": "UTC QSO calendar day in YYYY-MM-DD format",
        "expression": "strftime('%Y-%m-%d', c.\"start_time\")",
    },
    "hour": {
        "type": "integer",
        "description": "UTC QSO hour of day from 0 to 23",
        "expression": "CAST(strftime('%H', c.\"start_time\") AS INTEGER)",
    },
    "weekday": {
        "type": "integer",
        "description": "UTC ISO weekday from 1 (Monday) to 7 (Sunday)",
        "expression": "((CAST(strftime('%w', c.\"start_time\") AS INTEGER) + 6) % 7) + 1",
    },
}

DEFAULT_FIELDS = [
    "id",
    "datetime",
    "callsign",
    "band",
    "mode",
    "submode",
    "frequency",
    "rst_sent",
    "rst_received",
    "station_callsign",
]

BAND_FREQUENCY_COLUMNS = {"band": "freq", "band_rx": "freq_rx"}
BAND_LOOKUP_COLUMNS = {"bands.name", "bands.start_freq", "bands.end_freq"}

PROFILE_MATCH_COLUMNS = (
    ("operator_name", "my_name_intl", "string"),
    ("qth_name", "my_city_intl", "string"),
    ("iota", "my_iota", "string"),
    ("sota", "my_sota_ref", "string"),
    ("sig", "my_sig_intl", "string"),
    ("sig_info", "my_sig_info_intl", "string"),
    ("vucc", "my_vucc_grids", "string"),
    ("wwff", "my_wwff_ref", "string"),
    ("pota", "my_pota_ref", "string"),
    ("ituz", "my_itu_zone", "number"),
    ("cqz", "my_cq_zone", "number"),
    ("dxcc", "my_dxcc", "number"),
    ("county", "my_cnty", "string"),
    ("operator_callsign", "operator", "string"),
    ("darc_dok", "my_darc_dok", "string"),
)


class QsoQuery:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def supported_field_names() -> list[str]:
        return list(QSO_FIELDS)

    async def compile_value_set(
        self,
        connection: aiosqlite.Connection,
        scope: LogScope,
        filters: FilterGroup | None,
        field_name: str,
    ) -> tuple[list[str], list[Any]]:
        """Compile CTEs containing distinct QSO field values and their QSO counts."""
        await self._register_sql_functions(connection)
        columns = await self._available_columns(connection)
        self._require_contacts(columns)
        field = QSO_FIELDS.get(field_name)
        if field is None:
            raise InvalidQueryError(f"Unknown QSO field: {field_name}")
        if not field.available(columns):
            raise IncompatibleDatabaseError(
                f"QSO field {field_name!r} is unavailable in this QLog database schema"
            )

        parameters: list[Any] = []
        where_parts = await self._compile_scope(connection, scope, parameters, columns)
        if filters is not None:
            where_parts.append(self._compile_group(filters, parameters, columns))
        where_sql = " AND ".join(f"({part})" for part in where_parts) or "1 = 1"
        value = self._query_field(field_name, columns).base_expression()
        source = f"contacts AS c{self._autovalue_join(columns)}"

        if field.cardinality == "many":
            ctes = [
                (
                    '"_qso_rows" AS ('
                    'SELECT c."id" AS "_contact_id", '
                    f"qlog_list_values('{field_name}', {value}) || char(31) AS \"_rest\" "
                    f"FROM {source} WHERE {where_sql})"
                ),
                (
                    '"_qso_split"("_contact_id", "_rest", "_key") AS ('
                    'SELECT "_contact_id", "_rest", NULL FROM "_qso_rows" UNION ALL '
                    'SELECT "_contact_id", substr("_rest", instr("_rest", char(31)) + 1), '
                    'substr("_rest", 1, instr("_rest", char(31)) - 1) '
                    'FROM "_qso_split" WHERE "_rest" <> \'\')'
                ),
                (
                    '"_qso_items" AS ('
                    'SELECT DISTINCT "_contact_id", "_key" FROM "_qso_split" '
                    'WHERE NULLIF(TRIM(CAST("_key" AS TEXT)), \'\') IS NOT NULL)'
                ),
                (
                    '"_qso_values" AS ('
                    'SELECT MIN("_key") AS "_key", COUNT(*) AS "_qso_count" '
                    'FROM "_qso_items" GROUP BY "_key" COLLATE NOCASE)'
                ),
            ]
            return ctes, parameters

        key = (
            f"NULLIF(TRIM(CAST({value} AS TEXT)), '')"
            if field.value_type == "string"
            else value
        )
        collation = " COLLATE NOCASE" if field.value_type == "string" else ""
        return [
            (
                '"_qso_rows" AS ('
                'SELECT c."id" AS "_contact_id", '
                f'{key} AS "_key" FROM {source} WHERE {where_sql})'
            ),
            (
                '"_qso_values" AS ('
                'SELECT MIN("_key") AS "_key", COUNT(*) AS "_qso_count" '
                'FROM "_qso_rows" '
                'WHERE NULLIF(TRIM(CAST("_key" AS TEXT)), \'\') IS NOT NULL '
                f'GROUP BY "_key"{collation})'
            ),
        ], parameters

    async def schema(self) -> dict[str, Any]:
        async with self.database.connect() as connection:
            columns = await self._available_columns(connection)
            self._require_contacts(columns)
            band_ranges = await self._band_ranges(connection, columns)

        available_fields = {
            name: field for name, field in QSO_FIELDS.items() if field.available(columns)
        }
        fields = {
            name: {
                "type": field.value_type,
                "description": field.description,
                "cardinality": field.cardinality,
                "filterable": field.filterable,
                "sortable": field.sortable,
                "operators": (
                    [operator.value for operator in field.operators()]
                    if field.filterable
                    else []
                ),
                "aggregate_functions": [
                    function.value for function in field.aggregate_functions()
                ],
                **(
                    {
                        "enum": {
                            "name": field.enum_name,
                            "values": dict(field.enum_values),
                        }
                    }
                    if field.enum_values is not None
                    else {}
                ),
                **({"item_type": field.item_type} if field.item_type is not None else {}),
                **(
                    {
                        "list_semantics": {
                            "matching": _LIST_SYNTAXES[name].matching,
                            "explosion": _LIST_SYNTAXES[name].explosion,
                        }
                    }
                    if name in _LIST_SYNTAXES
                    else {}
                ),
                **({"side": field.side} if field.side is not None else {}),
                **(
                    {"paired_field": field.paired_field}
                    if field.paired_field in available_fields
                    else {}
                ),
            }
            for name, field in available_fields.items()
        }
        return {
            "fields": fields,
            "default_fields": [name for name in DEFAULT_FIELDS if name in fields],
            "filter_logic": ["and", "or"],
            "filter_group_negation": True,
            "filter_operator_semantics": {
                operator.value: FILTER_OPERATOR_DESCRIPTIONS[operator]
                for operator in FilterOperator
            },
            "band_frequency_filtering": {
                "whole_band": (
                    "Use band for a whole amateur band, including approximate frequency "
                    "references such as '14 MHz'. The band value includes QSOs that store "
                    "only band, only frequency, or both."
                ),
                "exact_frequency": (
                    "Use frequency for an exact transmit frequency such as 14.145 MHz. "
                    "Band-only QSOs cannot match an exact frequency."
                ),
                "subrange": (
                    "Use frequency with between or comparison operators for a subrange; "
                    "only QSOs with a stored exact frequency can match."
                ),
                "bands": band_ranges,
            },
            "list_values": {
                "operators": {
                    "has": "Match one requested semantic item.",
                    "has_any": "Match at least one item from a non-empty requested list.",
                    "has_all": "Match every item from a non-empty requested list.",
                },
                "normalization": (
                    "Matching is case-insensitive and ignores outer whitespace and whitespace "
                    "around separators defined by the field's ADIF list syntax."
                ),
                "raw_projection": (
                    "qso.query returns the original stored list string; normalization applies "
                    "only to matching and exploded aggregation."
                ),
                "explosion": (
                    "Exploded aggregation returns uppercase semantic identities and removes "
                    "duplicate identities within each source QSO."
                ),
                "malformed_values": (
                    "Malformed items remain available as trimmed exact items. Use contains "
                    "only as an explicit diagnostic fallback for inconsistent legacy data."
                ),
            },
            "aggregation": {
                "processing_order": [
                    "scope and top-level filters",
                    "requested list expansion",
                    "one_per_group",
                    "group_by and metrics",
                    "having",
                    "order and limit",
                ],
                "one_per_group": (
                    "Optionally keep the deterministic first or last QSO for each caller-"
                    "supplied key before metrics. If a key field is also exploded in group_by, "
                    "its individual semantic item is used in the key."
                ),
                "composite_distinct_count": (
                    "Use fields with 1 to 10 scalar semantic field names. Values are compared "
                    "as typed tuples; a QSO is excluded if any component is null or empty, and "
                    "text components are case-insensitive. Use field for one value or fields "
                    "for a tuple, never both."
                ),
                "functions": {
                    function.value: AGGREGATE_FUNCTION_DESCRIPTIONS[function]
                    for function in AggregateFunction
                },
                "group_by": {
                    "all_fields": True,
                    "explode_list_fields": True,
                    "bucket_intervals": [interval.value for interval in GroupInterval],
                    "interval_semantics": {
                        interval.value: GROUP_INTERVAL_DESCRIPTIONS[interval]
                        for interval in GroupInterval
                    },
                    "derived": {
                        name: {
                            "type": dimension["type"],
                            "description": dimension["description"],
                        }
                        for name, dimension in DERIVED_GROUP_BY.items()
                    },
                },
                "default_limit": 100,
                "max_limit": 1000,
                "default_order": "first metric descending, then group_by fields ascending",
                "having_operators": [operator.value for operator in HavingOperator],
                "having_operator_semantics": {
                    operator.value: FILTER_OPERATOR_DESCRIPTIONS[
                        FilterOperator(operator.value)
                    ]
                    for operator in HavingOperator
                },
            },
        }

    async def context(self) -> dict[str, Any]:
        async with self.database.connect() as connection:
            columns = await self._table_columns(connection, "contacts")
            self._require_contacts(columns)

            row = await self._fetch_one(
                connection,
                "SELECT COUNT(*) AS qso_count, "
                "strftime('%Y-%m-%dT%H:%M:%SZ', MIN(start_time)) AS first_qso, "
                "strftime('%Y-%m-%dT%H:%M:%SZ', MAX(start_time)) AS last_qso "
                "FROM contacts",
            )
            station_callsigns = (
                await self._station_callsign_locations(
                    connection, include_grid="my_gridsquare" in columns
                )
                if "station_callsign" in columns
                else []
            )
            operators = (
                await self._distinct_values(connection, "operator")
                if "operator" in columns
                else []
            )
            schema_version = await self._schema_version(connection)
            profiles = await self._station_profiles(connection)

        return {
            "database_schema": schema_version,
            "qso_count": row["qso_count"],
            "first_qso": row["first_qso"],
            "last_qso": row["last_qso"],
            "station_callsigns": station_callsigns,
            "operators": operators,
            "station_profiles": profiles,
        }

    async def query(
        self,
        scope: LogScope,
        filters: FilterGroup | None,
        fields: list[str] | None,
        one_per_group: OnePerGroup | None,
        sort: list[SortSpec] | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        self._validate_page(limit, offset)

        async with self.database.connect() as connection:
            await self._register_sql_functions(connection)
            columns = await self._available_columns(connection)
            self._require_contacts(columns)
            selected_names = self._resolve_fields(fields, columns)

            parameters: list[Any] = []
            where_parts: list[str] = []
            where_parts.extend(await self._compile_scope(connection, scope, parameters, columns))
            if filters is not None:
                where_parts.append(self._compile_group(filters, parameters, columns))

            select_sql = ", ".join(
                f'{self._query_field(name, columns).select_expression()} AS "{name}"'
                for name in selected_names
            )
            where_sql = " AND ".join(f"({part})" for part in where_parts) or "1 = 1"
            order_sql = self._compile_sort(sort, columns)
            autovalue_join = self._autovalue_join(columns)
            query_prefix = ""
            if one_per_group is not None:
                group_expressions = self._resolve_group_by(one_per_group.fields, columns)
                partition_sql = ", ".join(
                    expression for _, expression in group_expressions
                )
                query_prefix = (
                    'WITH "_one_per_group" AS ('
                    + self._ranked_rows_sql(
                        one_per_group,
                        'c."id" AS "_contact_id"',
                        partition_sql,
                        f"contacts AS c{autovalue_join}",
                        where_sql,
                    )
                    + ") "
                )
                where_sql = (
                    'c."id" IN (SELECT "_contact_id" FROM "_one_per_group" '
                    'WHERE "_rank" = 1)'
                )
            sql = (
                f"{query_prefix}SELECT {select_sql} FROM contacts AS c{autovalue_join} "
                f"WHERE {where_sql} ORDER BY {order_sql} LIMIT ? OFFSET ?"
            )
            parameters.extend((limit + 1, offset))

            rows = await self._execute_sql(connection, sql, parameters)

        has_more = len(rows) > limit
        items = [self._serialize_row(row, selected_names) for row in rows[:limit]]
        return {
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

    async def aggregate(
        self,
        scope: LogScope,
        filters: FilterGroup | None,
        one_per_group: OnePerGroup | None,
        group_by: list[str | GroupBySpec],
        metrics: list[AggregateMetric],
        having: list[AggregateHaving] | None,
        order_by: list[AggregateSort] | None,
        limit: int,
    ) -> dict[str, Any]:
        if not metrics:
            raise InvalidQueryError("metrics must contain at least one aggregate metric")
        if not 1 <= limit <= 1000:
            raise InvalidQueryError("limit must be between 1 and 1000")

        async with self.database.connect() as connection:
            await self._register_sql_functions(connection)
            columns = await self._available_columns(connection)
            self._require_contacts(columns)
            group_parameters: list[Any] = []
            group_expressions, exploded_fields = self._resolve_aggregate_group_by(
                group_by, columns, group_parameters
            )
            group_names = [name for name, _ in group_expressions]
            self._validate_metric_names(group_names, metrics)

            metric_parameters: list[Any] = []
            metric_sql = [
                f'{self._compile_aggregate_metric(metric, metric_parameters, columns)} '
                f'AS "{metric.output_name}"'
                for metric in metrics
            ]
            where_parameters: list[Any] = []
            where_parts = await self._compile_scope(
                connection, scope, where_parameters, columns
            )
            if filters is not None:
                where_parts.append(self._compile_group(filters, where_parameters, columns))
            where_sql = " AND ".join(f"({part})" for part in where_parts) or "1 = 1"

            prefix, from_sql, main_where = self._aggregate_source(
                exploded_fields, one_per_group, where_sql, columns
            )
            parameters = (
                where_parameters + group_parameters + metric_parameters
                if prefix
                else group_parameters + metric_parameters + where_parameters
            )

            select_groups = [
                f'{expression} AS "{name}"' for name, expression in group_expressions
            ]
            group_sql = (
                " GROUP BY "
                + ", ".join(str(index) for index in range(1, len(group_expressions) + 1))
                if group_expressions
                else ""
            )
            having_parameters: list[Any] = []
            having_sql = self._compile_having(having, metrics, having_parameters)
            order_sql = self._compile_aggregate_sort(order_by, group_names, metrics)
            sql = (
                f"{prefix}SELECT {', '.join(select_groups + metric_sql)} FROM {from_sql}"
                f"{main_where}{group_sql}{having_sql} ORDER BY {order_sql} LIMIT ?"
            )
            parameters.extend(having_parameters)
            parameters.append(limit + 1)

            rows = await self._execute_sql(connection, sql, parameters)

        has_more = len(rows) > limit
        result_rows = [
            {name: row[name] for name in group_names + [metric.output_name for metric in metrics]}
            for row in rows[:limit]
        ]
        boolean_groups = [
            item
            for item in group_by
            if isinstance(item, str)
            and item in QSO_FIELDS
            and QSO_FIELDS[item].value_type == "boolean"
        ]
        for row in result_rows:
            for name in boolean_groups:
                row[name] = bool(row[name])
        if "extra_fields" in group_names:
            for row in result_rows:
                if isinstance(row["extra_fields"], str):
                    try:
                        row["extra_fields"] = json.loads(row["extra_fields"])
                    except json.JSONDecodeError:
                        pass

        return {
            "rows": result_rows,
            "group_by": group_names,
            "metrics": [metric.output_name for metric in metrics],
            "effective_scope": scope.model_dump(
                mode="json", exclude_defaults=True, exclude_none=True
            ),
            "limit": limit,
            "returned": len(result_rows),
            "truncated": has_more,
        }

    def _resolve_aggregate_group_by(
        self,
        group_by: list[str | GroupBySpec],
        columns: set[str],
        parameters: list[Any],
    ) -> tuple[list[tuple[str, str]], list[str]]:
        names: set[str] = set()
        exploded_fields: list[str] = []
        for item in group_by:
            name = item if isinstance(item, str) else item.output_name
            if name in names:
                raise InvalidQueryError(f"Duplicate group_by field: {name}")
            names.add(name)
            if isinstance(item, GroupBySpec) and item.explode:
                self._require_list_field(item.field, columns)
                if item.field not in exploded_fields:
                    exploded_fields.append(item.field)

        aliases = {field: f"_item_{index}" for index, field in enumerate(exploded_fields)}
        expressions: list[tuple[str, str]] = []
        for item in group_by:
            if isinstance(item, GroupBySpec) and item.explode:
                alias = aliases[item.field]
                expressions.append((item.output_name, f'x."{alias}" COLLATE NOCASE'))
            elif isinstance(item, GroupBySpec):
                field = QSO_FIELDS.get(item.field)
                if field is None:
                    raise InvalidQueryError(f"Unknown QSO group_by field: {item.field}")
                if not field.available(columns):
                    raise IncompatibleDatabaseError(
                        f"QSO field {item.field!r} is unavailable in this QLog database schema"
                    )
                expressions.append(
                    (
                        item.output_name,
                        self._bucket_expression(item, field, columns, parameters),
                    )
                )
            else:
                expressions.extend(self._resolve_group_by([item], columns))
        return expressions, exploded_fields

    def _aggregate_source(
        self,
        exploded_fields: list[str],
        one_per_group: OnePerGroup | None,
        where_sql: str,
        columns: set[str],
    ) -> tuple[str, str, str]:
        """Build the filtered, optionally expanded and ranked aggregate row source."""
        autovalue_join = self._autovalue_join(columns)
        ctes: list[str] = []
        source = f"contacts AS c{autovalue_join}"
        source_where = where_sql
        item_columns = ""
        if exploded_fields:
            ctes.append(
                '"_base" AS ('
                'SELECT c."id" AS "_contact_id" '
                f"FROM contacts AS c{autovalue_join} WHERE {where_sql})"
            )
            for index, field_name in enumerate(exploded_fields):
                split_name = f"_list_{index}"
                items_name = f"_items_{index}"
                list_value = self._query_field(field_name, columns).base_expression()
                ctes.append(
                    f'"{split_name}"("_contact_id", "_rest", "_item") AS ('
                    'SELECT b."_contact_id", '
                    f"qlog_list_values('{field_name}', {list_value}) || char(31), NULL "
                    'FROM "_base" AS b JOIN contacts AS c '
                    'ON c."id" = b."_contact_id"'
                    f"{autovalue_join} UNION ALL "
                    'SELECT "_contact_id", '
                    'substr("_rest", instr("_rest", char(31)) + 1), '
                    'substr("_rest", 1, instr("_rest", char(31)) - 1) '
                    f'FROM "{split_name}" WHERE "_rest" <> \'\')'
                )
                ctes.append(
                    f'"{items_name}" AS ('
                    'SELECT DISTINCT "_contact_id", "_item" '
                    f'FROM "{split_name}" WHERE NULLIF("_item", \'\') IS NOT NULL)'
                )

            item_joins = "".join(
                f' JOIN "_items_{index}" AS "_item_{index}" '
                f'ON "_item_{index}"."_contact_id" = c."id"'
                for index in range(len(exploded_fields))
            )
            item_columns = "".join(
                f', "_item_{index}"."_item" AS "_item_{index}"'
                for index in range(len(exploded_fields))
            )
            source = (
                '"_base" AS b JOIN contacts AS c '
                'ON c."id" = b."_contact_id"'
                f"{autovalue_join}{item_joins}"
            )
            source_where = ""

        if one_per_group is not None:
            exploded_aliases = {
                field_name: f'"_item_{index}"."_item" COLLATE NOCASE'
                for index, field_name in enumerate(exploded_fields)
            }
            partition_sql = ", ".join(
                exploded_aliases.get(name, expression)
                for name, expression in self._resolve_group_by(
                    one_per_group.fields, columns
                )
            )
            rows_sql = self._ranked_rows_sql(
                one_per_group,
                f'c."id" AS "_contact_id"{item_columns}',
                partition_sql,
                source,
                source_where,
                [f'"_item_{index}"."_item"' for index in range(len(exploded_fields))],
            )
        elif exploded_fields:
            rows_sql = (
                f'SELECT c."id" AS "_contact_id"{item_columns} FROM {source}'
            )
        else:
            return "", source, f" WHERE {source_where}"

        ctes.append(f'"_rows" AS ({rows_sql})')
        prefix = ("WITH RECURSIVE " if exploded_fields else "WITH ") + ", ".join(ctes) + " "
        source = (
            '"_rows" AS x JOIN contacts AS c ON c."id" = x."_contact_id"'
            f"{autovalue_join}"
        )
        main_where = ' WHERE x."_rank" = 1' if one_per_group is not None else ""
        return prefix, source, main_where

    @staticmethod
    def _ranked_rows_sql(
        one_per_group: OnePerGroup,
        identity_sql: str,
        partition_sql: str,
        source_sql: str,
        where_sql: str,
        final_ties: list[str] | None = None,
    ) -> str:
        """Generate the deterministic ROW_NUMBER selection shared by query and aggregate."""
        direction = "ASC" if one_per_group.keep == "first" else "DESC"
        tie_sql = "".join(f", {expression} ASC" for expression in final_ties or [])
        where_clause = f" WHERE {where_sql}" if where_sql else ""
        return (
            f"SELECT {identity_sql}, ROW_NUMBER() OVER (PARTITION BY {partition_sql} "
            f'ORDER BY datetime(c."start_time") {direction}, c."id" {direction}{tie_sql}) '
            f'AS "_rank" FROM {source_sql}{where_clause}'
        )

    @staticmethod
    async def _table_columns(connection: aiosqlite.Connection, table: str) -> set[str]:
        async with connection.execute(f'PRAGMA table_info("{table}")') as cursor:
            return {row[1] for row in await cursor.fetchall()}

    @staticmethod
    async def _register_sql_functions(connection: aiosqlite.Connection) -> None:
        await connection.create_function("qlog_list_has", 3, _list_has, deterministic=True)
        await connection.create_function(
            "qlog_list_values", 2, _list_values, deterministic=True
        )
        await connection.create_function(
            "qlog_list_distinct_count", 2, _list_distinct_count, deterministic=True
        )
        await connection.create_function(
            "qlog_typed_tuple", -1, _typed_tuple, deterministic=True
        )

    @staticmethod
    async def _available_columns(connection: aiosqlite.Connection) -> set[str]:
        columns = await QsoQuery._table_columns(connection, "contacts")
        autovalue_columns = await QsoQuery._table_columns(connection, "contacts_autovalue")
        columns.update(f"contacts_autovalue.{column}" for column in autovalue_columns)
        band_columns = await QsoQuery._table_columns(connection, "bands")
        columns.update(f"bands.{column}" for column in band_columns)
        return columns

    @staticmethod
    async def _band_ranges(
        connection: aiosqlite.Connection, columns: set[str]
    ) -> list[dict[str, str | float]]:
        required = {"bands.name", "bands.start_freq", "bands.end_freq"}
        if not required.issubset(columns):
            return []
        async with connection.execute(
            'SELECT "name", "start_freq", "end_freq" FROM bands '
            'WHERE "start_freq" IS NOT NULL AND "end_freq" IS NOT NULL '
            'ORDER BY "start_freq", "end_freq", "name"'
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            {
                "name": row["name"],
                "start_mhz": row["start_freq"],
                "end_mhz": row["end_freq"],
            }
            for row in rows
        ]

    @staticmethod
    def _require_contacts(columns: set[str]) -> None:
        required = {"id", "start_time", "callsign"}
        if missing := required - columns:
            raise IncompatibleDatabaseError(
                "The selected database is not a supported QLog database; "
                f"contacts is missing: {', '.join(sorted(missing))}"
            )

    @staticmethod
    def _query_field(name: str, columns: set[str]) -> QsoField:
        field = QSO_FIELDS[name]
        if name in {"is_split", "is_cross_band"}:
            tx_band = QsoQuery._query_field("band", columns).base_expression()
            rx_band = QsoQuery._query_field("band_rx", columns).base_expression()
            tx_band = f"NULLIF(TRIM(CAST({tx_band} AS TEXT)), '')"
            rx_band = f"NULLIF(TRIM(CAST({rx_band} AS TEXT)), '')"
            cross_band = f"({tx_band} COLLATE NOCASE <> {rx_band})"
            if name == "is_cross_band":
                return replace(
                    field,
                    sql_expression=f"CASE WHEN {cross_band} THEN 1 ELSE 0 END",
                )

            tx_frequency = (
                'CAST(NULLIF(TRIM(CAST({table_alias}."freq" AS TEXT)), \'\') AS REAL)'
            )
            rx_frequency = (
                'CAST(NULLIF(TRIM(CAST({table_alias}."freq_rx" AS TEXT)), \'\') AS REAL)'
            )
            frequency_split = (
                f"(ROUND(ABS(({rx_frequency} - {tx_frequency}) * 1000000), 6) >= 1)"
            )
            return replace(
                field,
                sql_expression=(
                    f"CASE WHEN {cross_band} OR {frequency_split} THEN 1 ELSE 0 END"
                ),
            )

        frequency_column = BAND_FREQUENCY_COLUMNS.get(name)
        if (
            frequency_column is None
            or frequency_column not in columns
            or not BAND_LOOKUP_COLUMNS.issubset(columns)
        ):
            return field

        band = f'{{table_alias}}."{field.column}"'
        frequency = f'{{table_alias}}."{frequency_column}"'
        lookup = (
            '(SELECT NULLIF(TRIM(CAST(b."name" AS TEXT)), \'\') '
            "FROM bands AS b "
            f"WHERE CAST(NULLIF(TRIM(CAST({frequency} AS TEXT)), '') AS REAL) "
            'BETWEEN b."start_freq" AND b."end_freq" LIMIT 1)'
        )
        return replace(
            field,
            sql_expression=(
                f"COALESCE(NULLIF(TRIM(CAST({band} AS TEXT)), ''), {lookup})"
            ),
        )

    @staticmethod
    def _validate_page(limit: int, offset: int) -> None:
        if not 1 <= limit <= 1000:
            raise InvalidQueryError("limit must be between 1 and 1000")
        if offset < 0:
            raise InvalidQueryError("offset must be zero or greater")

    @staticmethod
    def _resolve_fields(fields: list[str] | None, columns: set[str]) -> list[str]:
        requested = (
            [name for name in DEFAULT_FIELDS if QSO_FIELDS[name].available(columns)]
            if fields is None
            else fields
        )
        if not requested:
            raise InvalidQueryError("fields must contain at least one semantic field")

        result: list[str] = []
        for name in requested:
            if name in result:
                continue
            field = QSO_FIELDS.get(name)
            if field is None:
                raise InvalidQueryError(f"Unknown QSO field: {name}")
            if not field.available(columns):
                raise IncompatibleDatabaseError(
                    f"QSO field {name!r} is unavailable in this QLog database schema"
                )
            result.append(name)
        return result

    @staticmethod
    def _resolve_group_by(
        group_by: list[str], columns: set[str]
    ) -> list[tuple[str, str]]:
        result: list[tuple[str, str]] = []
        seen: set[str] = set()
        for name in group_by:
            if name in seen:
                raise InvalidQueryError(f"Duplicate group_by field: {name}")
            seen.add(name)

            if dimension := DERIVED_GROUP_BY.get(name):
                result.append((name, dimension["expression"]))
                continue

            field = QSO_FIELDS.get(name)
            if field is None:
                raise InvalidQueryError(f"Unknown QSO group_by field: {name}")
            if not field.available(columns):
                raise IncompatibleDatabaseError(
                    f"QSO field {name!r} is unavailable in this QLog database schema"
                )
            result.append(
                (name, QsoQuery._query_field(name, columns).aggregate_expression())
            )
        return result

    @staticmethod
    def _require_list_field(name: str, columns: set[str]) -> QsoField:
        field = QSO_FIELDS.get(name)
        if field is None:
            raise InvalidQueryError(f"Unknown QSO list field: {name}")
        if not field.available(columns):
            raise IncompatibleDatabaseError(
                f"QSO field {name!r} is unavailable in this QLog database schema"
            )
        if field.cardinality != "many":
            raise InvalidQueryError(f"explode requires a list-valued QSO field: {name}")
        return QsoQuery._query_field(name, columns)

    @staticmethod
    def _bucket_expression(
        bucket: GroupBySpec,
        field: QsoField,
        columns: set[str],
        parameters: list[Any],
    ) -> str:
        field = QsoQuery._query_field(bucket.field, columns)
        if bucket.interval is not None:
            if field.value_type not in {"date", "datetime"}:
                raise InvalidQueryError(
                    f"Time interval requires a date or datetime field: {bucket.field}"
                )
            if bucket.interval in {
                GroupInterval.MINUTE,
                GroupInterval.HOUR,
            } and field.value_type != "datetime":
                raise InvalidQueryError(
                    f"{bucket.interval.value.capitalize()} interval requires a datetime field: "
                    f"{bucket.field}"
                )

            value = field.base_expression()
            if bucket.interval == GroupInterval.MINUTE:
                seconds = bucket.size * 60  # validated by GroupBySpec
                timestamp = QsoQuery._lower_bucket_expression(
                    f"CAST(strftime('%s', {value}) AS REAL)", seconds, parameters
                )
                return (
                    f"strftime('%Y-%m-%dT%H:%M:00Z', {timestamp}, 'unixepoch')"
                )
            if bucket.interval == GroupInterval.HOUR:
                return f"strftime('%Y-%m-%dT%H:00:00Z', {value})"
            if bucket.interval == GroupInterval.DAY:
                return f"strftime('%Y-%m-%d', {value})"
            if bucket.interval == GroupInterval.WEEK:
                offset = f"((CAST(strftime('%w', {value}) AS INTEGER) + 6) % 7)"
                return f"date({value}, '-' || {offset} || ' days')"
            if bucket.interval == GroupInterval.MONTH:
                return f"strftime('%Y-%m', {value})"
            if bucket.interval == GroupInterval.QUARTER:
                quarter = f"((CAST(strftime('%m', {value}) AS INTEGER) - 1) / 3 + 1)"
                return f"strftime('%Y', {value}) || '-Q' || {quarter}"
            return f"strftime('%Y', {value})"

        if field.value_type not in {"integer", "number"}:
            raise InvalidQueryError(
                f"Numeric bucket requires an integer or number field: {bucket.field}"
            )
        size = bucket.bucket_size
        value = field.aggregate_expression()
        value = QsoQuery._lower_bucket_expression(value, size, parameters)
        return f"ROUND({value}, 12)"

    @staticmethod
    def _lower_bucket_expression(
        value: str, size: float, parameters: list[Any]
    ) -> str:
        """Return the lower fixed-width boundary, including for negative values."""
        scaled = f"ROUND({value} / ?, 12)"
        bucket_index = (
            f"(CAST({scaled} AS INTEGER) - "
            f"({scaled} < CAST({scaled} AS INTEGER)))"
        )
        parameters.extend((size, size, size, size))
        return f"({bucket_index} * ?)"

    @staticmethod
    def _validate_metric_names(
        group_by: list[str], metrics: list[AggregateMetric]
    ) -> None:
        names = set(group_by)
        for metric in metrics:
            if metric.output_name in names:
                raise InvalidQueryError(
                    f"Duplicate aggregate result field: {metric.output_name}"
                )
            names.add(metric.output_name)

    async def _compile_scope(
        self,
        connection: aiosqlite.Connection,
        scope: LogScope,
        parameters: list[Any],
        columns: set[str],
    ) -> list[str]:
        parts: list[str] = []
        if scope.station_scope == StationScope.CALLSIGN:
            parts.append(
                self._compile_station_callsigns(
                    scope.station_callsigns, parameters, columns
                )
            )
        elif scope.station_scope == StationScope.PROFILE:
            parts.append(
                await self._compile_station_profiles(
                    connection, scope.station_profile_names, parameters, columns
                )
            )
        if scope.operator_callsigns:
            operator = QSO_FIELDS["operator"]
            if not operator.available(columns):
                raise IncompatibleDatabaseError(
                    "Operator filtering is unavailable; contacts is missing: operator"
                )
            parameters.extend(scope.operator_callsigns)
            placeholders = ", ".join("?" for _ in scope.operator_callsigns)
            parts.append(
                f"{operator.comparison_expression()} IN ({placeholders})"
            )
        if scope.date_from is not None:
            parts.append('datetime(c."start_time") >= datetime(?)')
            parameters.append(scope.date_from.isoformat())
        if scope.date_to is not None:
            parts.append("datetime(c.\"start_time\") < datetime(?, '+1 day')")
            parameters.append(scope.date_to.isoformat())
        return parts

    @staticmethod
    def _compile_station_callsigns(
        stations: list[StationCallsignSelector],
        parameters: list[Any],
        columns: set[str],
    ) -> str:
        required_columns = {"station_callsign"}
        if any(station.grid is not None for station in stations):
            required_columns.add("my_gridsquare")

        if missing := required_columns - columns:
            raise IncompatibleDatabaseError(
                "Station callsign filtering is unavailable; contacts is missing: "
                + ", ".join(sorted(missing))
            )

        callsign_expression = QSO_FIELDS["station_callsign"].comparison_expression()
        grid_expression = QSO_FIELDS["my_grid"].comparison_expression()

        parts: list[str] = []
        for station in stations:
            if station.grid is None:
                parts.append(f"{callsign_expression} = ?")
                parameters.append(station.callsign)
            else:
                parts.append(
                    f"({callsign_expression} = ? AND {grid_expression} = ?)"
                )
                parameters.extend((station.callsign, station.grid))

        return "(" + " OR ".join(parts) + ")"

    async def _compile_station_profiles(
        self,
        connection: aiosqlite.Connection,
        profile_names: list[str],
        parameters: list[Any],
        contact_columns: set[str],
    ) -> str:
        required_contact_columns = {"station_callsign", "my_gridsquare"}
        if missing := required_contact_columns - contact_columns:
            raise IncompatibleDatabaseError(
                "Station-profile filtering is unavailable; contacts is missing: "
                + ", ".join(sorted(missing))
            )

        profile_columns = await self._table_columns(connection, "station_profiles")
        required_profile_columns = {"profile_name", "callsign", "locator"}
        if missing := required_profile_columns - profile_columns:
            raise IncompatibleDatabaseError(
                "Station-profile filtering is unavailable; station_profiles is missing: "
                + ", ".join(sorted(missing))
            )

        placeholders = ", ".join("?" for _ in profile_names)
        parameters.extend(profile_names)
        matches = [
            'c."station_callsign" COLLATE NOCASE = sp."callsign"',
            'c."my_gridsquare" COLLATE NOCASE = sp."locator"',
        ]
        for profile_column, contact_column, value_type in PROFILE_MATCH_COLUMNS:
            if profile_column not in profile_columns or contact_column not in contact_columns:
                continue
            if value_type == "number":
                matches.append(
                    f'(COALESCE(sp."{profile_column}", 0) = 0 '
                    f'OR c."{contact_column}" = sp."{profile_column}")'
                )
            else:
                matches.append(
                    f'(NULLIF(TRIM(sp."{profile_column}"), \'\') IS NULL '
                    f'OR c."{contact_column}" COLLATE NOCASE = sp."{profile_column}")'
                )

        return (
            "EXISTS (SELECT 1 FROM station_profiles AS sp "
            f'WHERE sp."profile_name" COLLATE NOCASE IN ({placeholders}) '
            f"AND {' AND '.join(matches)})"
        )

    def _compile_group(
        self,
        group: FilterGroup,
        parameters: list[Any],
        columns: set[str],
    ) -> str:
        parts = [self._compile_condition(item, parameters, columns) for item in group.conditions]
        parts.extend(self._compile_group(item, parameters, columns) for item in group.groups)
        expression = f" {group.logic.value.upper()} ".join(f"({part})" for part in parts)
        expression = expression or "1 = 1"
        return f"NOT ({expression})" if group.negate else expression

    def _compile_condition(
        self,
        condition: FilterCondition,
        parameters: list[Any],
        columns: set[str],
    ) -> str:
        field = QSO_FIELDS.get(condition.field)
        if field is None or not field.filterable:
            raise InvalidQueryError(f"Unknown or non-filterable QSO field: {condition.field}")
        if not field.available(columns):
            raise IncompatibleDatabaseError(
                f"QSO field {condition.field!r} is unavailable in this QLog database schema"
            )
        field = self._query_field(condition.field, columns)

        operator = condition.op
        if operator not in field.operators():
            raise InvalidQueryError(
                f"Operator {operator.value!r} is not supported for QSO field "
                f"{condition.field!r}"
            )

        expression = field.comparison_expression()
        value = condition.value

        if operator in LIST_OPERATORS:
            if operator == FilterOperator.HAS:
                if not isinstance(value, str) or not value.strip():
                    raise InvalidQueryError("has requires one non-empty string value")
                requested = [value]
                joiner = " AND "
            else:
                if (
                    not isinstance(value, list)
                    or not value
                    or any(not isinstance(item, str) or not item.strip() for item in value)
                ):
                    raise InvalidQueryError(
                        f"{operator.value} requires a non-empty list of non-empty strings"
                    )
                requested = value
                joiner = " OR " if operator == FilterOperator.HAS_ANY else " AND "

            parameters.extend(requested)
            matches = [
                f"qlog_list_has('{condition.field}', {field.base_expression()}, ?)"
                for _ in requested
            ]
            return "(" + joiner.join(matches) + ")"

        if operator == FilterOperator.IS_NULL:
            return f"{expression} IS NULL"
        if operator == FilterOperator.IS_NOT_NULL:
            return f"{expression} IS NOT NULL"
        if operator in {FilterOperator.IS_EMPTY, FilterOperator.IS_NOT_EMPTY}:
            empty_expression = f"NULLIF(TRIM(CAST({field.base_expression()} AS TEXT)), '')"
            keyword = "IS NULL" if operator == FilterOperator.IS_EMPTY else "IS NOT NULL"
            return f"{empty_expression} {keyword}"
        if operator == FilterOperator.EQ and value is None:
            return f"{expression} IS NULL"
        if operator == FilterOperator.NEQ and value is None:
            return f"{expression} IS NOT NULL"

        if operator in {FilterOperator.IN, FilterOperator.NOT_IN}:
            if not isinstance(value, list) or not value:
                raise InvalidQueryError(f"{operator.value} requires a non-empty value list")
            placeholders = ", ".join(field.comparison_placeholder() for _ in value)
            parameters.extend(value)
            keyword = "IN" if operator == FilterOperator.IN else "NOT IN"
            return f"{expression} {keyword} ({placeholders})"

        if operator == FilterOperator.BETWEEN:
            if not isinstance(value, list) or len(value) != 2:
                raise InvalidQueryError("between requires exactly two values")
            parameters.extend(value)
            placeholder = field.comparison_placeholder()
            return f"{expression} BETWEEN {placeholder} AND {placeholder}"

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

        sql_operators = {
            FilterOperator.EQ: "=",
            FilterOperator.NEQ: "<>",
            FilterOperator.GT: ">",
            FilterOperator.GTE: ">=",
            FilterOperator.LT: "<",
            FilterOperator.LTE: "<=",
        }
        if operator not in sql_operators:
            raise InvalidQueryError(f"Unsupported filter operator: {operator.value}")
        parameters.append(value)
        return f"{expression} {sql_operators[operator]} {field.comparison_placeholder()}"

    def _compile_aggregate_metric(
        self,
        metric: AggregateMetric,
        parameters: list[Any],
        columns: set[str],
    ) -> str:
        metric_filter = (
            self._compile_group(metric.filters, parameters, columns)
            if metric.filters is not None
            else None
        )
        if metric.function == AggregateFunction.COUNT:
            if metric_filter is None:
                return "COUNT(*)"
            return f"COUNT(CASE WHEN {metric_filter} THEN 1 END)"

        if metric.fields is not None:
            expressions: list[str] = []
            for name in metric.fields:
                field = QSO_FIELDS.get(name)
                if field is None:
                    raise InvalidQueryError(f"Unknown QSO aggregate field: {name}")
                if not field.available(columns):
                    raise IncompatibleDatabaseError(
                        f"QSO field {name!r} is unavailable in this QLog database schema"
                    )
                if field.cardinality != "one" or field.value_type == "object":
                    raise InvalidQueryError(
                        f"Composite distinct_count requires scalar QSO fields: {name}"
                    )
                field = self._query_field(name, columns)
                expression = field.aggregate_expression()
                expressions.append(
                    f"LOWER({expression})" if field.value_type == "string" else expression
                )

            required = " AND ".join(
                f"({expression}) IS NOT NULL" for expression in expressions
            )
            if metric_filter is not None:
                required = f"({metric_filter}) AND {required}"
            return (
                "COUNT(DISTINCT CASE WHEN "
                f"{required} THEN qlog_typed_tuple({', '.join(expressions)}) END)"
            )

        if metric.explode:
            field_name = metric.field or ""
            field = self._require_list_field(field_name, columns)
            expression = field.base_expression()
            if metric_filter is not None:
                expression = f"CASE WHEN {metric_filter} THEN {expression} END"
            delimiter = _LIST_SYNTAXES[field_name].delimiter
            return (
                f"qlog_list_distinct_count('{field_name}', "
                f"GROUP_CONCAT({expression}, '{delimiter}'))"
            )

        field = self._aggregate_field(metric, columns)

        expression = field.aggregate_expression()
        if metric_filter is not None:
            expression = f"CASE WHEN {metric_filter} THEN {expression} END"
        if metric.function == AggregateFunction.DISTINCT_COUNT:
            return f"COUNT(DISTINCT {expression})"
        return f"{metric.function.value.upper()}({expression})"

    def _aggregate_field(
        self, metric: AggregateMetric, columns: set[str]
    ) -> QsoField:
        field_name = metric.field or ""
        field = QSO_FIELDS.get(field_name)
        if field is None:
            raise InvalidQueryError(f"Unknown QSO aggregate field: {metric.field}")
        if not field.available(columns):
            raise IncompatibleDatabaseError(
                f"QSO field {metric.field!r} is unavailable in this QLog database schema"
            )
        field = self._query_field(field_name, columns)
        if metric.function not in field.aggregate_functions():
            raise InvalidQueryError(
                f"Aggregate function {metric.function.value!r} is not supported for "
                f"QSO field {metric.field!r}"
            )
        return field

    @staticmethod
    def _compile_aggregate_sort(
        order_by: list[AggregateSort] | None,
        group_by: list[str],
        metrics: list[AggregateMetric],
    ) -> str:
        allowed = set(group_by) | {metric.output_name for metric in metrics}
        requested = order_by or [
            AggregateSort(field=metrics[0].output_name, direction=SortDirection.DESC)
        ]

        parts: list[str] = []
        sorted_fields: set[str] = set()
        for item in requested:
            if item.field not in allowed:
                raise InvalidQueryError(
                    f"Unknown aggregate order_by field: {item.field}"
                )
            if item.field in sorted_fields:
                continue
            parts.append(f'"{item.field}" {item.direction.value.upper()}')
            sorted_fields.add(item.field)
        for name in group_by:
            if name not in sorted_fields:
                parts.append(f'"{name}" ASC')
        return ", ".join(parts)

    @staticmethod
    def _compile_having(
        having: list[AggregateHaving] | None,
        metrics: list[AggregateMetric],
        parameters: list[Any],
    ) -> str:
        if not having:
            return ""

        aliases = {metric.output_name for metric in metrics}
        operators = {
            HavingOperator.EQ: "=",
            HavingOperator.NEQ: "<>",
            HavingOperator.GT: ">",
            HavingOperator.GTE: ">=",
            HavingOperator.LT: "<",
            HavingOperator.LTE: "<=",
        }
        parts = []
        for condition in having:
            if condition.field not in aliases:
                raise InvalidQueryError(
                    f"Unknown aggregate having field: {condition.field}"
                )
            parts.append(f'"{condition.field}" {operators[condition.op]} ?')
            parameters.append(condition.value)
        return " HAVING " + " AND ".join(parts)

    @staticmethod
    def _autovalue_join(columns: set[str]) -> str:
        if "contacts_autovalue.contactid" not in columns:
            return ""
        return ' LEFT JOIN contacts_autovalue AS a ON a."contactid" = c."id"'

    @staticmethod
    def _compile_sort(sort: list[SortSpec] | None, columns: set[str]) -> str:
        requested = sort or [SortSpec(field="datetime", direction=SortDirection.DESC)]
        parts: list[str] = []
        sorted_fields: set[str] = set()
        for item in requested:
            field = QSO_FIELDS.get(item.field)
            if field is None or not field.sortable:
                raise InvalidQueryError(f"Unknown or non-sortable QSO field: {item.field}")
            if not field.available(columns):
                raise IncompatibleDatabaseError(
                    f"QSO field {item.field!r} is unavailable in this QLog database schema"
                )
            field = QsoQuery._query_field(item.field, columns)
            parts.append(f"{field.comparison_expression()} {item.direction.value.upper()}")
            sorted_fields.add(item.field)
        if "id" not in sorted_fields:
            parts.append('c."id" DESC')
        return ", ".join(parts)

    @staticmethod
    def _serialize_row(row: aiosqlite.Row, fields: list[str]) -> dict[str, Any]:
        result = {name: row[name] for name in fields}
        for name in fields:
            if QSO_FIELDS[name].value_type == "boolean":
                result[name] = bool(result[name])
        if isinstance(result.get("extra_fields"), str):
            try:
                result["extra_fields"] = json.loads(result["extra_fields"])
            except json.JSONDecodeError:
                pass
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

    @staticmethod
    async def _fetch_one(connection: aiosqlite.Connection, sql: str) -> aiosqlite.Row:
        async with connection.execute(sql) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise IncompatibleDatabaseError("QLog database query returned no context row")
        return row

    @staticmethod
    async def _station_callsign_locations(
        connection: aiosqlite.Connection,
        *,
        include_grid: bool,
    ) -> list[dict[str, str | None]]:
        if not include_grid:
            sql = (
                'SELECT DISTINCT TRIM("station_callsign") COLLATE NOCASE AS callsign '
                'FROM contacts '
                'WHERE NULLIF(TRIM("station_callsign"), \'\') IS NOT NULL '
                'ORDER BY callsign COLLATE NOCASE'
            )
            async with connection.execute(sql) as cursor:
                return [{"callsign": row[0], "grid": None} for row in await cursor.fetchall()]

        sql = (
            'SELECT DISTINCT TRIM("station_callsign") COLLATE NOCASE AS callsign, '
            'NULLIF(TRIM("my_gridsquare"), \'\') COLLATE NOCASE AS grid '
            'FROM contacts '
            'WHERE NULLIF(TRIM("station_callsign"), \'\') IS NOT NULL '
            'ORDER BY callsign COLLATE NOCASE, grid COLLATE NOCASE'
        )
        async with connection.execute(sql) as cursor:
            return [
                {"callsign": row[0], "grid": row[1]}
                for row in await cursor.fetchall()
            ]

    @staticmethod
    async def _distinct_values(connection: aiosqlite.Connection, column: str) -> list[str]:
        sql = (
            f'SELECT DISTINCT TRIM("{column}") COLLATE NOCASE AS value FROM contacts '
            f'WHERE NULLIF(TRIM("{column}"), \'\') IS NOT NULL ORDER BY value COLLATE NOCASE'
        )
        async with connection.execute(sql) as cursor:
            return [row[0] for row in await cursor.fetchall()]

    async def _schema_version(self, connection: aiosqlite.Connection) -> int | None:
        if not await self._table_columns(connection, "schema_versions"):
            return None
        row = await self._fetch_one(
            connection, "SELECT MAX(version) AS version FROM schema_versions"
        )
        return row["version"]

    async def _station_profiles(self, connection: aiosqlite.Connection) -> list[dict[str, Any]]:
        columns = await self._table_columns(connection, "station_profiles")
        if not {"profile_name", "callsign", "locator"}.issubset(columns):
            return []
        async with connection.execute(
            "SELECT profile_name AS name, callsign, locator AS grid "
            "FROM station_profiles ORDER BY profile_name COLLATE NOCASE"
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]
