# Architecture

QLog MCP is intentionally a thin, read-only semantic layer over a QLog SQLite
database. The client asks for a meaning such as “QSOs on 20 m” or “DXCC entities
present in my confirmed contacts”; it never supplies a table name, column name, or
SQL fragment. The server translates that meaning into a validated, parameterized
SQLite operation and returns facts that an assistant can explain.

## Request path

```text
MCP client
    |
    v
server.py (FastMCP tools, public contract, lazy construction)
    |
    +--> qso.py -----------+
    |                       |
    +--> catalog.py --------+--> database.py --> QLog SQLite (read-only)
              |
              +--> qso.py (scoped semantic key set for catalog.match_qso)
    |
    +--> usage_log.py (optional redacted middleware)
```

The normal lifecycle is:

1. `qlog.get_context` discovers the available dates, station callsigns, grids,
   operators, and profiles. This is the information needed to choose a scope.
2. `qlog.get_schema` describes the semantic fields and operations available in the
   current database. The snapshot is reusable for the connection.
3. `qso.query` or `qso.aggregate` compiles a scoped QSO request. Aggregation stays in
   SQLite, so summaries do not transfer the complete log.
4. `catalog.query` reads reference-directory facts, while `catalog.match_qso` compares
   a filtered catalog key set with a filtered QSO key set.
5. The assistant applies any external award, activity, or contest rules to those facts.

## Module responsibilities

| Module | Responsibility | Deliberately does not do |
| --- | --- | --- |
| `server.py` | FastMCP construction, tool names, descriptions, and dependency wiring | Open the database during startup or implement query logic |
| `qso.py` | QSO scope, semantic field registry, validation, SQL expressions, querying, aggregation | Expose physical SQLite names to clients |
| `catalog.py` | Catalog capability detection, catalog fields, catalog queries, and neutral set comparison | Decide whether a set is an award or contest result |
| `filters.py` | Shared nested filter models and operator vocabulary | Interpret domain-specific award rules |
| `database.py` | Lazy SQLite connections in read-only mode | Migrate, repair, write, or create a QLog database |
| `discovery.py` | CLI, environment, and QLog discovery-file precedence | Reimplement QLog's platform path logic |
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

`catalog.match_qso` asks `qso.py` to compile the complete scoped and filtered QSO key
set. `catalog.py` then compares that CTE with the filtered catalog in one SQLite
statement and calculates set summaries and pagination. The result is a neutral relation
such as “matched” or “not matched”; it is not an award decision.

## Source layout

```text
src/qlog_mcp/
├── server.py
├── qso.py
├── catalog.py
├── filters.py
├── database.py
├── discovery.py
├── errors.py
├── usage_log.py
└── __main__.py
```

The CLI entry point is `__main__.py`; the public MCP surface is assembled by
`server.create_server`. Tests exercise the same public tools through an in-process
FastMCP client and representative temporary QLog databases.
