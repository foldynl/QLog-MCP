"""Neutral SQL composition for comparisons between validated value sets."""

from __future__ import annotations

from enum import Enum


class SetRelation(str, Enum):
    """Relationship between the left and right value sets."""

    BOTH = "both"
    LEFT_ONLY = "left_only"
    RIGHT_ONLY = "right_only"
    EITHER = "either"


SET_RELATION_DESCRIPTIONS = {
    SetRelation.BOTH: "Keys present in both value sets.",
    SetRelation.LEFT_ONLY: "Keys present only in the left value set.",
    SetRelation.RIGHT_ONLY: "Keys present only in the right value set.",
    SetRelation.EITHER: "Keys present in either value set, including keys present in both.",
}


def compile_set_comparison(
    left_values: str,
    right_values: str,
    relation: SetRelation,
    *,
    case_insensitive: bool,
    partition_columns: list[str] | None = None,
) -> list[str]:
    """Build CTEs for set membership and complete relation counts.

    Both input CTEs must expose one distinct, non-null ``_key`` column and the same
    optional partition columns. Their names are trusted identifiers supplied by the
    server, never user input.
    """
    relation = SetRelation(relation)
    partitions = partition_columns or []
    equality_parts = [f'l."{name}" IS r."{name}"' for name in partitions]
    equality_parts.append('l."_key" = r."_key"')
    if case_insensitive:
        equality_parts[-1] = 'l."_key" COLLATE NOCASE = r."_key" COLLATE NOCASE'
    equality = " AND ".join(equality_parts)

    left_partitions = "".join(f'l."{name}" AS "{name}", ' for name in partitions)
    right_partitions = "".join(f'r."{name}" AS "{name}", ' for name in partitions)
    partition_names = ", ".join(f'"{name}"' for name in partitions)
    summary_group = f" GROUP BY {partition_names}" if partitions else ""
    relation_columns = f"{partition_names}, " if partitions else ""

    relation_where = {
        SetRelation.BOTH: '"_left_present" = 1 AND "_right_present" = 1',
        SetRelation.LEFT_ONLY: '"_left_present" = 1 AND "_right_present" = 0',
        SetRelation.RIGHT_ONLY: '"_left_present" = 0 AND "_right_present" = 1',
        SetRelation.EITHER: "1 = 1",
    }[relation]

    return [
        (
            '"_set_values" AS ('
            f'SELECT {left_partitions}l."_key" AS "_key", 1 AS "_left_present", '
            'CASE WHEN r."_key" IS NULL THEN 0 ELSE 1 END AS "_right_present" '
            f'FROM "{left_values}" AS l LEFT JOIN "{right_values}" AS r ON {equality} '
            "UNION ALL "
            f'SELECT {right_partitions}r."_key" AS "_key", 0 AS "_left_present", '
            '1 AS "_right_present" '
            f'FROM "{right_values}" AS r LEFT JOIN "{left_values}" AS l ON {equality} '
            'WHERE l."_key" IS NULL)'
        ),
        (
            f'"_set_summary" AS (SELECT {relation_columns}'
            'COALESCE(SUM("_left_present"), 0) AS "left_values", '
            'COALESCE(SUM("_right_present"), 0) AS "right_values", '
            'COALESCE(SUM("_left_present" * "_right_present"), 0) AS "both_values", '
            'COALESCE(SUM("_left_present" * (1 - "_right_present")), 0) '
            'AS "left_only_values", '
            'COALESCE(SUM((1 - "_left_present") * "_right_present"), 0) '
            'AS "right_only_values", '
            f'COUNT(*) AS "either_values" FROM "_set_values"{summary_group})'
        ),
        (
            f'"_set_relation_values" AS (SELECT {relation_columns}"_key" '
            'FROM "_set_values" '
            f"WHERE {relation_where})"
        ),
    ]
