"""FastMCP server and its public tools."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from .catalog import (
    CatalogMatchSort,
    CatalogName,
    CatalogQuery,
    CatalogSort,
    MatchRelation,
    MembershipMatchBasis,
    MembershipMatchRelation,
    MembershipMatchSort,
)
from .database import Database
from .discovery import discover_database
from .qso import (
    AggregateHaving,
    AggregateMetric,
    AggregateSort,
    FilterGroup,
    GroupBySpec,
    LogScope,
    OnePerGroup,
    QsoQuery,
    SortSpec,
)
from .usage_log import UsageLoggingMiddleware


def create_server(
    database_path: Path | None = None,
    usage_log_path: Path | None = None,
) -> FastMCP:
    """Create a configured QLog MCP server without opening the database."""
    database = Database(discover_database(database_path))
    qso = QsoQuery(database)
    catalogs = CatalogQuery(database, qso)

    # LLM clients otherwise tend to call qlog.get_schema before every operation even
    # though its capability snapshot normally stays valid for the server connection.
    server = FastMCP(
        name="QLog MCP",
        instructions=(
            "Before the first qso.query or qso.aggregate call, use qlog.get_context and ask "
            "the user to choose "
            "a station callsign (optionally a grid), a station profile, or all QSOs. Reuse the "
            "chosen scope until the user changes it. Use station_scope='all' only when the user "
            "explicitly chooses all QSOs. Prefer qso.aggregate for statistical questions so "
            "individual QSOs are not transferred. Call qlog.get_schema once for each domain "
            "when first needed and reuse that capability snapshot throughout the conversation. "
            "Do not call it before every operation; refresh it only after the MCP server or "
            "database changes, or after a compatibility error suggests that capabilities "
            "changed. Use the QSO schema to select group_by dimensions and functions supported "
            "by each metric field. For a "
            "list-valued field, use has/has_any/has_all for semantic item matching and "
            "explode=true when grouping or distinct-counting individual items. Use contains "
            "only to inspect malformed legacy list text. For a "
            "whole band or an approximate reference such as 14 MHz, filter by band; use "
            "frequency only for an exact frequency or subrange. Use "
            "the cached catalog schema before catalog.query or catalog.match_qso to discover "
            "which QLog reference directories, semantic fields, and QSO mappings are "
            "available. When a catalog has contacted- and logging-station mappings, use "
            "the QSO schema's side and paired_field metadata to choose the intended one. Use "
            "catalog.match_qso for present/absent set comparisons. Catalog data supplies facts "
            "for analysis; apply award or contest rules outside this server. Membership data "
            "covers only club lists the user downloaded into QLog, not all clubs. Before "
            "analyzing a named club, verify it in membership_clubs; if absent, do not infer "
            "non-membership or use membership fields for that club. Membership catalogs do not "
            "support catalog.match_qso. Use membership.match_qso for worked or not-worked "
            "roster callsigns during recorded membership, member_clubs_at_qso_date only when "
            "the external rule uses the stored membership interval, and "
            "member_clubs_in_directory only for the local snapshot."
        ),
    )
    if usage_log_path is not None:
        server.add_middleware(UsageLoggingMiddleware(usage_log_path))

    @server.tool(
        name="qlog.get_context",
        description=(
            "Return the log date range and the available station callsign/grid pairs, "
            "operators, and station profiles. Call this before the first QSO operation."
        ),
    )
    async def get_context() -> Annotated[
        dict[str, Any],
        Field(
            description=(
                "Object containing database_schema, qso_count, first_qso and last_qso UTC "
                "timestamps, station_callsigns with optional grids, operators, and "
                "station_profiles for choosing a QSO scope."
            )
        ),
    ]:
        return await qso.context()

    @server.tool(
        name="qlog.get_capabilities",
        description="Return capabilities supported by this QLog MCP server.",
    )
    def get_capabilities() -> Annotated[
        dict[str, Any],
        Field(
            description=(
                "Object containing server, stage, database_configured, QSO support flags and "
                "semantic field names, supported catalog operations and names, and membership "
                "roster matching. Membership data remains limited to lists downloaded into QLog; "
                "use qlog.get_schema to verify database fields and catalogs."
            )
        ),
    ]:
        return {
            "server": "qlog-mcp",
            "stage": "qso-query",
            "database_configured": database.path is not None,
            "qso": {
                "query": True,
                "aggregate": True,
                "fields": qso.supported_field_names(),
            },
            "catalog": {
                "query": True,
                "match_qso": True,
                "names": catalogs.supported_catalog_names(),
            },
            "membership": {"match_qso": True},
        }

    @server.tool(
        name="qlog.get_schema",
        description=(
            "Describe available semantic fields and operations for QSO or catalog data. Call "
            "once per needed domain for the current server connection and reuse the result; "
            "repeat only after the server or database changes, or after a compatibility error."
        ),
    )
    async def get_schema(
        domain: Annotated[
            Literal["qso", "catalog"],
            Field(description="Schema domain to describe: 'qso' or 'catalog'."),
        ] = "qso",
    ) -> Annotated[
        dict[str, Any],
        Field(
            description=(
                "Object keyed by domain. The qso schema describes QSO fields, filtering, and "
                "aggregation. The catalog schema lists available reference directories, "
                "their semantic fields, operators, defaults, source capability, and compatible "
                "QSO fields. Treat this as a reusable capability snapshot for the current "
                "server connection."
            )
        ),
    ]:
        if domain == "catalog":
            return {domain: await catalogs.schema()}
        return {domain: await qso.schema()}

    @server.tool(
        name="catalog.query",
        description=(
            "Search a QLog reference directory through semantic fields. This returns catalog "
            "facts and does not apply award, activity, or contest rules."
        ),
    )
    async def catalog_query(
        catalog: Annotated[
            CatalogName,
            Field(
                description=(
                    "Semantic catalog name advertised by qlog.get_schema(domain='catalog')."
                )
            ),
        ],
        filters: Annotated[
            FilterGroup | None,
            Field(
                description=(
                    "Optional nested filters using operators advertised for fields of the "
                    "selected catalog."
                )
            ),
        ] = None,
        fields: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Semantic catalog fields to return, in order. Omit to use that catalog's "
                    "default_fields from qlog.get_schema."
                )
            ),
        ] = None,
        sort: Annotated[
            list[CatalogSort] | None,
            Field(
                description=(
                    "Semantic catalog sort fields and directions. Omit to use that catalog's "
                    "deterministic default_order from qlog.get_schema."
                )
            ),
        ] = None,
        limit: Annotated[
            int,
            Field(
                ge=1,
                le=1000,
                description="Maximum number of catalog entries to return, from 1 to 1000.",
            ),
        ] = 100,
        offset: Annotated[
            int,
            Field(
                ge=0,
                description="Zero-based row offset; use page.next_offset for the next page.",
            ),
        ] = 0,
    ) -> Annotated[
        dict[str, Any],
        Field(
            description=(
                "Object with catalog, items, fields, and page. Page contains limit, offset, "
                "returned, has_more, and next_offset; use next_offset only when has_more is "
                "true."
            )
        ),
    ]:
        return await catalogs.query(catalog, filters, fields, sort, limit, offset)

    @server.tool(
        name="catalog.match_qso",
        description=(
            "Compare a filtered reference catalog with distinct values from a scoped, "
            "filtered QSO population without returning individual QSOs. Call qlog.get_schema "
            "for both catalog and QSO domains first. Relations describe only set membership; "
            "this tool does not apply award, activity, or contest rules."
        ),
    )
    async def catalog_match_qso(
        catalog: Annotated[
            CatalogName,
            Field(
                description=(
                    "Semantic catalog name advertised by qlog.get_schema(domain='catalog')."
                )
            ),
        ],
        qso_field: Annotated[
            str,
            Field(
                description=(
                    "Semantic QSO field listed in compatible_qso_fields for the selected "
                    "catalog. Use side and paired_field in the QSO schema to choose the "
                    "contacted- or logging-station field intended by the question. List-valued "
                    "fields are compared as normalized individual items."
                )
            ),
        ],
        scope: Annotated[
            LogScope,
            Field(
                description=(
                    "Required station selection and optional date/operator limits applied "
                    "before QSO keys are compared."
                )
            ),
        ],
        qso_filters: Annotated[
            FilterGroup | None,
            Field(
                description=(
                    "Optional nested QSO filters using fields and operators from the QSO "
                    "schema. Use these to define the QSO population being compared."
                )
            ),
        ] = None,
        catalog_filters: Annotated[
            FilterGroup | None,
            Field(
                description=(
                    "Optional nested filters using fields and operators advertised for the "
                    "selected catalog. These define the catalog population being compared."
                )
            ),
        ] = None,
        relation: Annotated[
            MatchRelation,
            Field(
                description=(
                    "Set relation to return: matched catalog keys present in QSOs, "
                    "not_matched catalog keys absent from QSOs, or qso_only QSO keys absent "
                    "from the filtered catalog. Choose from the question's intended set; these "
                    "relations do not by themselves mean worked, confirmed, needed, or valid."
                )
            ),
        ] = MatchRelation.MATCHED,
        fields: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Catalog fields returned for matched and not_matched; omit for catalog "
                    "defaults. They do not select QSO fields. For qso_only omit this parameter "
                    "or provide both key and qso_count; no QSO content is returned."
                )
            ),
        ] = None,
        sort: Annotated[
            list[CatalogMatchSort] | None,
            Field(
                description=(
                    "Catalog fields and directions for matched or not_matched. For qso_only "
                    "only key and qso_count are sortable. Omit for key ascending."
                )
            ),
        ] = None,
        limit: Annotated[
            int,
            Field(
                ge=1,
                le=1000,
                description="Maximum number of comparison details to return, from 1 to 1000.",
            ),
        ] = 100,
        offset: Annotated[
            int,
            Field(
                ge=0,
                description="Zero-based detail offset; use page.next_offset for the next page.",
            ),
        ] = 0,
    ) -> Annotated[
        dict[str, Any],
        Field(
            description=(
                "Object with relation, fields, effective_scope, page, and items. For matched "
                "and not_matched, items are projected catalog rows; for qso_only, each item has "
                "only key and qso_count. Summary counts distinct non-empty keys in the complete "
                "filtered sets, not QSO rows, and is unaffected by detail pagination. It always "
                "contains catalog_values, matched_values, not_matched_values, and "
                "qso_only_values. Only qso_count is a QSO occurrence count."
            )
        ),
    ]:
        return await catalogs.match_qso(
            catalog,
            qso_field,
            scope,
            qso_filters,
            catalog_filters,
            relation,
            fields,
            sort,
            limit,
            offset,
        )

    @server.tool(
        name="membership.match_qso",
        description=(
            "Compare one club list downloaded into QLog, not a global club directory, with a "
            "scoped QSO population. A QSO matches only when the contacted base callsign is "
            "covered by the stored membership interval on that QSO date. This is evidence, not "
            "award-credit determination."
        ),
    )
    async def membership_match_qso(
        club: Annotated[
            str,
            Field(
                min_length=1,
                description=(
                    "Club identifier from membership_clubs. The tool rejects a club whose list "
                    "is not downloaded into QLog rather than inferring non-membership."
                ),
            ),
        ],
        scope: Annotated[
            LogScope,
            Field(description="Required station selection and optional QSO date/operator limits."),
        ],
        qso_filters: Annotated[
            FilterGroup | None,
            Field(description="Optional semantic filters defining the QSO population."),
        ] = None,
        member_as_of: Annotated[
            date | None,
            Field(
                description=(
                    "Optional date selecting roster records valid on that day. It does not "
                    "replace the per-QSO membership-date test, and malformed non-empty date "
                    "boundaries remain excluded."
                )
            ),
        ] = None,
        membership_basis: Annotated[
            MembershipMatchBasis,
            Field(
                description=(
                    "qso_date requires membership on each QSO date; directory_snapshot matches "
                    "any QSO against the locally stored roster and intentionally ignores dates."
                )
            ),
        ] = MembershipMatchBasis.QSO_DATE,
        relation: Annotated[
            MembershipMatchRelation,
            Field(
                description=(
                    "Return roster callsigns with at least one matching QSO, or roster callsigns "
                    "with none, under membership_basis."
                )
            ),
        ] = MembershipMatchRelation.WORKED,
        sort: Annotated[
            list[MembershipMatchSort] | None,
            Field(description="Optional ordering of returned callsign summaries."),
        ] = None,
        limit: Annotated[
            int,
            Field(ge=1, le=1000, description="Maximum detail rows to return, from 1 to 1000."),
        ] = 100,
        offset: Annotated[
            int,
            Field(description="Zero-based detail offset; use page.next_offset for the next page."),
        ] = 0,
    ) -> Annotated[
        dict[str, Any],
        Field(
            description=(
                "Object with complete member/QSO summary, paginated callsign rows, and the "
                "effective QSO scope. summary.member_callsigns is the unique roster; "
                "worked_callsigns and not_worked_callsigns cover that complete roster, not just "
                "this page; matching_qsos counts distinct QSO rows; invalid_membership_records "
                "counts malformed dates; and excluded_invalid_membership_records is nonzero only "
                "when qso_date excludes them."
            )
        ),
    ]:
        return await catalogs.match_membership_qso(
            club,
            scope,
            qso_filters,
            member_as_of,
            membership_basis,
            relation,
            sort,
            limit,
            offset,
        )

    @server.tool(
        name="qso.query",
        description=(
            "Search individual QSOs with a required station scope and optional semantic "
            "filters, projection, sorting, and pagination. List fields support exact semantic "
            "item operators; contains remains a raw-text diagnostic fallback."
        ),
    )
    async def query(
        scope: Annotated[
            LogScope,
            Field(description="Required station selection and optional date/operator limits."),
        ],
        filters: Annotated[
            FilterGroup | None,
            Field(description="Optional nested filters using fields from qlog.get_schema."),
        ] = None,
        fields: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Semantic fields to return, in order. Omit to use the default projection "
                    "reported by qlog.get_schema."
                )
            ),
        ] = None,
        one_per_group: Annotated[
            OnePerGroup | None,
            Field(
                description=(
                    "Optionally keep the first or last matching QSO in each group before "
                    "sorting and pagination."
                )
            ),
        ] = None,
        sort: Annotated[
            list[SortSpec] | None,
            Field(
                description=(
                    "Semantic sort fields and directions. Omit for newest QSO first."
                )
            ),
        ] = None,
        limit: Annotated[
            int,
            Field(
                ge=1,
                le=1000,
                description="Maximum number of QSOs to return, from 1 to 1000.",
            ),
        ] = 100,
        offset: Annotated[
            int,
            Field(
                ge=0,
                description="Zero-based row offset; use page.next_offset for the next page.",
            ),
        ] = 0,
    ) -> Annotated[
        dict[str, Any],
        Field(
            description=(
                "Object with items (projected QSO objects), fields (their semantic field "
                "order), effective_scope, and page. Page contains limit, offset, returned, "
                "has_more, and next_offset; use next_offset only when has_more is true."
            )
        ),
    ]:
        return await qso.query(
            scope, filters, fields, one_per_group, sort, limit, offset
        )

    @server.tool(
        name="qso.aggregate",
        description=(
            "Calculate server-side QSO statistics without returning individual QSOs. "
            "It supports caller-defined first/last deduplication, composite distinct values, "
            "and fixed-size UTC time buckets. Use qlog.get_schema for field-specific "
            "aggregate functions and exact processing semantics."
        ),
    )
    async def aggregate(
        scope: Annotated[
            LogScope,
            Field(description="Required station selection and optional date/operator limits."),
        ],
        group_by: Annotated[
            list[str | GroupBySpec],
            Field(
                description=(
                    "Group dimensions. A string groups by a semantic field's complete value "
                    "or by a derived time dimension. An object applies exactly one interval, "
                    "numeric bucket_size, or list explode=true and requires a unique as name. "
                    "Use an empty list for one overall row."
                ),
            ),
        ],
        metrics: Annotated[
            list[AggregateMetric],
            Field(
                min_length=1,
                description=(
                    "Metrics calculated for every group. Each requires a unique as name; "
                    "qlog.get_schema lists functions supported by each field."
                ),
            ),
        ],
        filters: Annotated[
            FilterGroup | None,
            Field(
                description="Optional nested filters applied to all aggregate metrics."
            ),
        ] = None,
        one_per_group: Annotated[
            OnePerGroup | None,
            Field(
                description=(
                    "Optionally retain the deterministic first or last QSO for each supplied "
                    "key after scope, top-level filtering, and requested list expansion, but "
                    "before metrics are calculated."
                )
            ),
        ] = None,
        having: Annotated[
            list[AggregateHaving] | None,
            Field(
                description=(
                    "Optional metric-alias comparisons applied after aggregation; "
                    "multiple conditions are combined with AND."
                )
            ),
        ] = None,
        order_by: Annotated[
            list[AggregateSort] | None,
            Field(
                description=(
                    "Optional ordering by group dimensions or metric aliases. Omit to sort "
                    "by the first metric descending and then by group dimensions."
                ),
            ),
        ] = None,
        limit: Annotated[
            int,
            Field(
                ge=1,
                le=1000,
                description="Maximum number of aggregate groups to return, from 1 to 1000.",
            ),
        ] = 100,
    ) -> Annotated[
        dict[str, Any],
        Field(
            description=(
                "Object with rows, group_by and metrics output-name lists, effective_scope, "
                "limit, returned, and truncated. Each row is keyed by the requested group "
                "and metric aliases; truncated=true means additional groups were omitted."
            )
        ),
    ]:
        return await qso.aggregate(
            scope,
            filters,
            one_per_group,
            group_by,
            metrics,
            having,
            order_by,
            limit,
        )

    return server


mcp = create_server()
