"""Shared request models for semantic filters and sorting."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

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
            "Value required by op: one scalar for ordinary comparisons; one non-empty "
            "string for has; a non-empty list for in, not_in, has_any, or has_all; exactly "
            "two values for between; omit for is_null, is_not_null, is_empty, or "
            "is_not_empty."
        ),
    )


class FilterGroup(BaseModel):
    """Recursive boolean combination of filter conditions and nested groups."""

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
