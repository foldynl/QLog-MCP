"""Shared request models for semantic filters and sorting."""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from .errors import InvalidQueryError

Scalar = str | int | float | bool


class FilterOperator(str, Enum):
    """Filter operation; each semantic field advertises its valid subset."""

    EQ = "eq"
    NEQ = "neq"
    IN = "in"
    NOT_IN = "not_in"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    BETWEEN = "between"
    CONTAINS = "contains"
    STARTS_WITH = "starts_with"
    ENDS_WITH = "ends_with"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"
    IS_EMPTY = "is_empty"
    IS_NOT_EMPTY = "is_not_empty"
    HAS = "has"
    HAS_ANY = "has_any"
    HAS_ALL = "has_all"


FILTER_OPERATOR_DESCRIPTIONS = {
    FilterOperator.EQ: (
        "Equal to one scalar value; text is case-insensitive. A null value matches SQL NULL."
    ),
    FilterOperator.NEQ: (
        "Not equal to one scalar value; text is case-insensitive. A null value matches "
        "non-NULL values."
    ),
    FilterOperator.IN: "Equal to any value in a non-empty list.",
    FilterOperator.NOT_IN: "Not equal to every value in a non-empty list.",
    FilterOperator.GT: "Greater than one scalar value.",
    FilterOperator.GTE: "Greater than or equal to one scalar value.",
    FilterOperator.LT: "Less than one scalar value.",
    FilterOperator.LTE: "Less than or equal to one scalar value.",
    FilterOperator.BETWEEN: "Between exactly two values, including both boundaries.",
    FilterOperator.CONTAINS: (
        "Contain the literal text anywhere, case-insensitively; '%' and '_' are not wildcards."
    ),
    FilterOperator.STARTS_WITH: "Start with the literal text, case-insensitively.",
    FilterOperator.ENDS_WITH: "End with the literal text, case-insensitively.",
    FilterOperator.IS_NULL: "Be SQL NULL; an empty string does not match.",
    FilterOperator.IS_NOT_NULL: "Be anything other than SQL NULL, including an empty string.",
    FilterOperator.IS_EMPTY: "Be SQL NULL, an empty string, or whitespace-only text.",
    FilterOperator.IS_NOT_EMPTY: "Contain a non-whitespace value.",
    FilterOperator.HAS: "Contain one exact normalized semantic list item.",
    FilterOperator.HAS_ANY: "Contain at least one item from a non-empty requested list.",
    FilterOperator.HAS_ALL: "Contain every item from a non-empty requested list.",
}


def compact_date_expression(value: str) -> str:
    """Normalize an internal YYYYMMDD SQL expression only when it is a real calendar date."""
    normalized = (
        f"SUBSTR({value}, 1, 4) || '-' || SUBSTR({value}, 5, 2) || '-' || "
        f"SUBSTR({value}, 7, 2)"
    )
    digits = "[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]"
    return (
        f"CASE WHEN NULLIF({value}, '') IS NULL THEN NULL "
        f"WHEN LENGTH({value}) = 8 AND {value} GLOB '{digits}' "
        f"AND DATE({normalized}, '+0 days') = {normalized} THEN {normalized} END"
    )


COMPARISON_OPERATORS = (
    FilterOperator.EQ,
    FilterOperator.NEQ,
    FilterOperator.IN,
    FilterOperator.NOT_IN,
    FilterOperator.GT,
    FilterOperator.GTE,
    FilterOperator.LT,
    FilterOperator.LTE,
    FilterOperator.BETWEEN,
)
TEXT_OPERATORS = (
    FilterOperator.CONTAINS,
    FilterOperator.STARTS_WITH,
    FilterOperator.ENDS_WITH,
)
PRESENCE_OPERATORS = (
    FilterOperator.IS_NULL,
    FilterOperator.IS_NOT_NULL,
    FilterOperator.IS_EMPTY,
    FilterOperator.IS_NOT_EMPTY,
)
LIST_OPERATORS = (
    FilterOperator.HAS,
    FilterOperator.HAS_ANY,
    FilterOperator.HAS_ALL,
)
BOOLEAN_OPERATORS = (
    FilterOperator.EQ,
    FilterOperator.NEQ,
    FilterOperator.IN,
    FilterOperator.NOT_IN,
)


class FilterLogic(str, Enum):
    """Boolean operation used to combine one filter group's members."""

    AND = "and"
    OR = "or"


class SortDirection(str, Enum):
    """Ascending or descending result order."""

    ASC = "asc"
    DESC = "desc"


class FilterCondition(BaseModel):
    """One semantic field comparison inside a filter group."""

    model_config = ConfigDict(extra="forbid")

    field: str = Field(
        description=(
            "Semantic field from the selected qlog.get_schema domain; that field also lists "
            "its valid operators."
        )
    )
    op: FilterOperator = Field(
        default=FilterOperator.EQ,
        description="Operator listed for this semantic field by qlog.get_schema.",
    )
    value: Scalar | list[Scalar] | None = Field(
        default=None,
        description=(
            "Match the field type from qlog.get_schema: finite JSON numbers for numeric "
            "fields (never booleans or numeric strings), JSON booleans for boolean fields, "
            "and strings for text. Dates use YYYY-MM-DD; datetime accepts that UTC date or "
            "an ISO date/time with T or space, optional seconds/fraction and Z or ±HH:MM "
            "offset; no offset means UTC. Object fields compare raw stored JSON text. "
            "Use one scalar for ordinary comparisons; one non-empty "
            "string for has; a non-empty list for in, not_in, has_any, or has_all; exactly "
            "two values for between. Every list item must match the field type. "
            "For is_null, is_not_null, is_empty, or is_not_empty, omit value or use null; "
            "other values are rejected. eq/neq with null test SQL NULL."
        ),
    )


class FilterGroup(BaseModel):
    """Recursive boolean combination of filter conditions and nested groups."""

    model_config = ConfigDict(extra="forbid")

    logic: FilterLogic = Field(
        default=FilterLogic.AND,
        description="How conditions and nested groups in this group are combined.",
    )
    conditions: list[FilterCondition] = Field(
        default_factory=list,
        description="Semantic field comparisons in this group.",
    )
    groups: list[FilterGroup] = Field(
        default_factory=list,
        description="Nested filter groups for more complex boolean expressions.",
    )
    negate: bool = Field(default=False, description="Negate the result of this filter group")


def validate_scalar_value(value: Scalar, value_type: str, context: str) -> None:
    """Validate a comparison operand without coercing its JSON type or exposing its value."""
    expected = "a JSON string"
    valid = isinstance(value, str)
    if value_type in {"integer", "number"}:
        expected = (
            "a finite JSON number, not a boolean or numeric string "
            "(integer parameters must fit signed 64 bits)"
        )
        valid = (
            type(value) is int and -(2**63) <= value < 2**63
            or type(value) is float and math.isfinite(value)
        )
    elif value_type == "boolean":
        expected = "a JSON boolean (true or false)"
        valid = type(value) is bool
    elif value_type in {"date", "datetime"}:
        expected = "a real date in YYYY-MM-DD format"
        pattern = r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
        if value_type == "datetime":
            expected = (
                "a valid ISO date/time (YYYY-MM-DD or YYYY-MM-DDTHH:MM[:SS[.fraction]], "
                "T or space, optional Z or ±HH:MM offset; no offset means UTC)"
            )
            pattern += (
                r"(?:[T ][0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]+)?)?"
                r"(?:Z|[+-](?:0[0-9]|1[0-4]):[0-5][0-9])?)?"
            )
        valid = False
        if isinstance(value, str) and re.fullmatch(pattern, value):
            try:
                if value_type == "date":
                    date.fromisoformat(value)
                else:
                    datetime.fromisoformat(value)
                valid = True
            except ValueError:
                pass
    if not valid:
        raise InvalidQueryError(f"{context} requires {expected}; see qlog.get_schema")


def validate_filter_value(condition: FilterCondition, value_type: str) -> None:
    """Check semantic operand types; compilers retain operator shape and SQL handling."""
    value = condition.value
    context = f"Filter {condition.field!r} with operator {condition.op.value!r}"
    if condition.op in PRESENCE_OPERATORS:
        if value is not None:
            raise InvalidQueryError(f"{context} does not accept a value; omit value or use null")
    elif value is not None:
        for item in value if isinstance(value, list) else [value]:
            validate_scalar_value(item, value_type, context)
