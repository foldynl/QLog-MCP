# Architecture

QLog MCP is intentionally a thin, read-only semantic layer over a QLog SQLite
database. The client asks for a meaning such as “QSOs on 20 m” or “DXCC entities
present in my confirmed contacts”; it never supplies a table name, column name, or
SQL fragment. The server translates that meaning into a validated, parameterized
SQLite operation and returns facts that an assistant can explain.

Live QLog state is a separate read-only path through the application's local runtime IPC.

## Request path

```text
MCP client
    |
    v
server.py (FastMCP tools, public contract, lazy construction)
    |
    +--> qso.py -----------------------> database.py --> QLog SQLite (read-only)
    |       +--> setops.py
    |
    +--> catalog.py -------------------> database.py
    |       +--> qso.py (scoped semantic key set for catalog.match_qso)
    |       +--> setops.py
    |
    +--> runtime.py -------------------> qlog-runtime --> running QLog
    |
    +--> usage_log.py (optional redacted middleware)
```

The normal lifecycle is:

1. `qlog.get_context` discovers the available dates, station callsigns, grids,
   operators, and profiles. This is the information needed to choose a scope.
2. `qlog.get_schema` describes the semantic fields and operations available in the
   current database. The snapshot is reusable for the connection.
3. `qso.query`, `qso.aggregate`, or `qso.compare_sets` compiles a scoped QSO request.
   Aggregation and set comparison stay in SQLite, so summaries do not transfer the
   complete log.
4. `catalog.query` reads reference-directory facts, while `catalog.match_qso` compares
   a filtered catalog key set with a filtered QSO key set.
5. The assistant applies any external award, activity, or contest rules to those facts.

## Module responsibilities

| Module | Responsibility | Deliberately does not do |
| --- | --- | --- |
| `server.py` | FastMCP construction, tool names, descriptions, and dependency wiring | Open the database during startup or implement query logic |
| `qso.py` | QSO scope, semantic field registry, validation, SQL expressions, querying, aggregation, and QSO value sets | Expose physical SQLite names to clients |
| `catalog.py` | Catalog capability detection, catalog fields, catalog queries, and neutral set comparison | Decide whether a set is an award or contest result |
| `setops.py` | SQL composition for relations and complete summaries between two validated value sets | Define scopes, fields, catalog rules, or public tools |
| `filters.py` | Shared nested filter models and operator vocabulary | Interpret domain-specific award rules |
| `database.py` | Lazy SQLite connections in read-only mode | Migrate, repair, write, or create a QLog database |
| `discovery.py` | CLI, environment, and QLog discovery-file precedence | Reimplement QLog's platform path logic |
| `runtime.py` | Lazy Unix socket calls, protocol validation, provider schema and live values | Open SQLite, cache live state, discover endpoints or declare provider fields |
| `usage_log.py` | Optional privacy-conscious JSONL request metrics | Persist QSO content or filter values |

There is no generic service or repository layer. The direct `server.py -> qso.py` /
`catalog.py -> database.py` path keeps ownership visible. A new layer is justified only
when two implemented areas need the same real behavior.

## Read-only and trust boundaries

The server opens SQLite lazily with a URI containing `mode=ro` and then enables
`PRAGMA query_only = ON`. Starting the MCP server, listing tools, and inspecting the
public capability surface therefore do not require a database and cannot create one.

Both query engines accept identifiers only from server-owned semantic registries. SQL
expressions, joins, aliases, sort directions, and aggregate functions are constructed
internally; user-provided values are bound parameters. This keeps the public API useful
without turning it into arbitrary SQL access.

The optional `contacts_autovalue` table is read through a one-to-one relationship,
`contacts_autovalue.contactid = contacts.id`, when present. The QSO registry checks
available columns before each operation, which lets the advertised capability surface
follow QLog schema evolution instead of hard-coding a QLog application release.

`qso.compare_sets` asks `qso.py` to compile two complete scoped and filtered QSO key sets.
`catalog.match_qso` compiles one catalog set and asks `qso.py` for the other. With
`partition_by`, `qso.py` first derives the bounded scalar partitions from the station scope,
then applies QSO filters inside each partition; the catalog set remains shared. Both paths use
the small `setops.py` SQL composer for set relations and complete summary counts, then add
their domain-specific detail rows and pagination. Results such as “left only” or “matched”
are neutral relations, not award or contest decisions.

## Runtime path and provider extension

`server.py` owns one stateless `RuntimeClient`. `qlog.list_live_sources`,
`qlog.get_live_context(sources=...)` and `qlog.get_schema(domain="runtime", sources=...)`
open a fresh connection to the fixed `qlog-runtime`
endpoint and verify protocol 1 before requesting data. Startup, tool listing and capability
inspection do not contact QLog. Connection and request execution share a two-second timeout.

QLog generates the source list from its provider registrations: each source has a stable
name and an English description. Listing sources and requesting schema read metadata only.
MCP forwards a requested `sources` list through IPC; Context selects providers before
sending requests to their owning threads. Unselected providers are not collected or awaited.
Omitted selection preserves the original all-provider behavior. An explicit empty list or
unknown name is rejected without reading state.

QLog collects the selected provider snapshots in their owning threads. MCP forwards the resulting `values`
and `issues`; a provider timeout is a partial result, while a transport timeout fails the call.
There is no MCP-side provider registry or mirrored radio state.

To expose another provider, implement and register it in QLog using its runtime provider API.
Register its source name and description once in QLog. Its schema supplies field names,
types, descriptions, source names and units, and its snapshot supplies
JSON values. No changes to `runtime.py` or `server.py` are needed for additional sources or fields.
Refresh the source list and load schema for newly needed sources; do not maintain a Python enum.
Transport or envelope changes require a compatible IPC protocol change.

## Source layout

```text
src/qlog_mcp/
├── server.py
├── qso.py
├── catalog.py
├── setops.py
├── filters.py
├── database.py
├── discovery.py
├── runtime.py
├── errors.py
├── usage_log.py
└── __main__.py
```

The CLI entry point is `__main__.py`; the public MCP surface is assembled by
`server.create_server`. Tests exercise the same public tools through an in-process
FastMCP client and representative temporary QLog databases.
