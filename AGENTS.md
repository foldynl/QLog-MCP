# AGENTS.md

## Project purpose

QLog MCP is a Python 3.12, read-only Model Context Protocol server for QLog SQLite
databases. Its public surface is a small semantic API for QSO context, schema, queries,
and aggregations. Keep changes narrow and preserve this read-only, semantic boundary.

## Repository map

- `src/qlog_mcp/server.py`: FastMCP construction and public tool definitions.
- `src/qlog_mcp/qso.py`: request models, semantic field registry, validation, SQL
  generation, querying, and aggregation.
- `src/qlog_mcp/database.py`: lazy read-only SQLite connections.
- `src/qlog_mcp/discovery.py`: CLI, environment, and QLog discovery-file lookup.
- `src/qlog_mcp/errors.py`: domain-specific errors.
- `src/qlog_mcp/usage_log.py`: privacy-preserving JSONL usage metrics.
- `src/qlog_mcp/__main__.py`: minimal CLI entry point.
- `tests/`: pytest coverage using temporary representative QLog databases and an
  in-process FastMCP client.
- `docs/`: architecture, public-tool, discovery, and compatibility contracts.
- `packaging/appimage/` and `.github/workflows/appimage.yml`: AppImage packaging
  through `uv` and native x86_64/aarch64 CI builds.

## Codebase discovery

This repository is indexed by codebase-memory-mcp as project `qlog-mcp`. Prefer graph
tools over filesystem search for structural code discovery.

1. At the start of structural work, call `list_projects` or `index_status` and confirm
   the project, generation, branch, and coverage state.
2. Use `search_graph` to locate symbols, `trace_path` for callers/callees, and
   `get_code_snippet` for exact implementations. Use `query_graph` or
   `get_architecture` only when the question needs broader relationships.
3. Default to Tier 2 verification: inspect exact snippets for material claims and
   paginate every relevant result set.
4. After candidate files are known, call `check_index_coverage` once for every file
   relied on. For negative or exhaustive claims, also check the relevant scopes.
5. Read the reported missed, stale, excluded, or unknown ranges directly before relying
   on graph results. A clean coverage result is best-effort, not proof of completeness.
6. Use `rg` or direct file reads for literals, error text, TOML/YAML, Markdown, shell
   scripts, generated metadata, and any graph coverage gap.

Before editing, inspect `git status` and preserve unrelated or pre-existing worktree
changes.

## Architectural invariants

- Never write to, migrate, repair, or create a QLog database. Connections must retain
  SQLite URI `mode=ro` and `PRAGMA query_only = ON`.
- Keep database discovery and opening lazy. Starting the server, listing tools, and
  inspecting capabilities must not require or create a database.
- Preserve discovery precedence: explicit `--database`, then `QLOG_DB_PATH`, then the
  documented `qlog-service.json` locations.
- Public tools expose semantic field names and capabilities, not physical tables,
  columns, joins, or arbitrary SQL. Accept identifiers only from server-owned registries
  and bind every user-provided value as a SQLite parameter.
- Keep query and aggregation behavior on the shared semantic-field, scope, filter, and
  validation path. Do not duplicate compilers or validation merely for a new tool.
- Every QSO operation requires an explicit station scope. Do not silently broaden a
  callsign or profile request to all QSOs.
- Preserve deterministic ordering, pagination limits, `has_more`/`next_offset`, and
  aggregation truncation semantics when changing query construction.
- Treat database compatibility as capabilities, not a hard-coded QLog release. Optional
  columns and tables must disappear cleanly from the advertised schema; explicit use of
  an unavailable semantic field must produce a clear compatibility error.
- Usage logs may record shapes, identifiers, timings, counts, parameter types, and SQL
  with placeholders. They must never record filter values, callsigns from scopes, QSO
  content, or untrusted exception text. Preserve restrictive file permissions and log
  rotation.
- Do not add placeholder award or SIG tools. Public tools should exist only when their
  behavior is implemented and documented.
- Keep the direct `server.py -> qso.py -> database.py` path. Add a service, repository,
  or other generic layer only after at least two implemented areas need the same real
  behavior.

## Implementation conventions

- Use Python 3.12 syntax, explicit type annotations, async database operations, and the
  existing domain-specific exception types.
- Ruff is authoritative; its configured line length is 100 characters.
- Public FastMCP tools and all public parameters/results need useful descriptions.
  Continue using typed Pydantic models and `Annotated[..., Field(...)]` constraints so
  MCP schemas remain self-describing.
- Put new QSO-facing fields in the semantic registry and define availability,
  expressions, operators, and aggregate support in one place. Do not leak physical
  names into tool arguments or responses.
- Validate identifiers before composing SQL. Only values belong in bound parameters;
  table names, column names, aliases, directions, and functions must come from trusted
  server-owned definitions.
- Prefer the smallest complete change in the existing module. Avoid speculative
  abstractions, duplicate state, and unrelated cleanup.
- Update `uv.lock` whenever project dependencies change. Do not hand-edit the lockfile.

## Tests and checks

Set up and run the same core checks as CI:

```bash
uv sync --locked
uv run pytest
uv run ruff check .
```

During development, run the narrowest relevant test first, for example:

```bash
uv run pytest tests/test_mcp.py -q
uv run pytest tests/test_aggregate.py -q
uv run pytest tests/test_discovery.py -q
uv run pytest tests/test_usage_log.py -q
```

- Add focused regression coverage for behavior changes. Exercise public tools through
  `fastmcp.Client(create_server(...))` when changing the MCP contract or end-to-end
  behavior.
- Extend the temporary SQLite fixture for representative current-schema behavior. Use a
  separate minimal temporary database when testing missing or older schema capabilities.
- Test both the successful path and meaningful validation/compatibility failures. For a
  public signature change, also assert generated tool descriptions and JSON-schema
  constraints.
- For usage logging changes, explicitly prove sensitive values and untrusted error text
  are absent from the written log.
- AppImage builds require Linux, `uv`, and network downloads; they do not require a
  container engine. Use `packaging/appimage/build-appimage.sh`, which runs the pinned
  `appimage.ctl` through `uv`, constrains packaged dependencies from `uv.lock`, creates
  a SHA-256 file, and smoke-tests the result. Build x86_64 and aarch64 artifacts on
  their native architectures.

## Documentation contract

Update documentation in the same change when behavior changes:

- `docs/tools.md` for public tools, fields, operators, limits, and result semantics.
- `docs/compatibility.md` for required or optional database capabilities.
- `docs/qlog-discovery.md` for path lookup or discovery-document changes.
- `docs/architecture.md` for module ownership or data-flow changes.
- `README.md` for setup, CLI, user-facing examples, logging, and packaging.

Keep examples aligned with the actual FastMCP schema and preserve the distinction between
whole-band filtering (`band`) and exact frequency/range filtering (`frequency`).

## Completion checklist

Before handing off a change:

1. Review the diff for accidental public-contract, privacy, or read-only regressions.
2. Run the relevant focused tests, then the full pytest and Ruff checks unless the task is
   documentation-only.
3. State exactly which checks ran and disclose anything not run, including AppImage
   builds.
