# QLog MCP

QLog MCP lets an AI assistant explore your [QLog](https://github.com/foldynl/QLog) logbook in plain language. It is a read-only
[Model Context Protocol](https://modelcontextprotocol.io/) server: your database is never changed, migrated, or repaired.

## What can I do with it?

Instead of learning an SQL schema, ask questions such as:

- “What was my last QSO, and which band and mode did I use?”
- “How many CW contacts did I make with Brazil in 2025?”
- “Which DXCC entities did I work last year, and which are confirmed by LoTW?”
- “Show my activity trend for the last six months, including the busiest bands and modes.”
- “Which Canadian provinces do I still need for Canadaward?”
- “What was my best period in the IOTA contest?”
- “Was this QSO uploaded to LoTW, eQSL, or another service?”

The assistant can combine QLog data with the rules of an award or contest to produce a
useful analysis: identify the missing entities, count confirmed contacts, find the first
or last qualifying QSO, compare bands and modes, and highlight gaps in the log. The
server supplies the recorded facts and catalog data; it does not pretend that a QSO is
officially credited when an external award body still needs to verify it.

Useful analyses remain practical even for large logs. Counts, trends, distinct DXCC
entities, duplicate selection, and other statistics are calculated inside SQLite, so the
assistant does not need to download every QSO just to answer a summary question.

### Good answers do not require a huge reasoning model

QLog MCP supplies the assistant with structured fields, explicit station scope, database
capabilities, and server-side aggregation. That means a compact or medium-sized model
can still produce useful, reproducible answers for many everyday logbook questions; it
does not need to rediscover the database structure or inspect thousands of QSOs one by
one. For complex award and contest questions, the result still depends on having the
correct external rules and on stating the assumptions used for confirmations, dates,
duplicates, and scoring.

## What the server exposes

The public tools are deliberately small and semantic:

- `qso.query` finds individual QSOs using fields such as date, callsign, band, mode,
  DXCC, grid, contest, and confirmation/upload status.
- `qso.aggregate` calculates counts, distinct values, trends, and grouped statistics
  without returning the underlying QSOs.
- `catalog.query` searches available POTA, SOTA, WWFF, IOTA, and DXCC directories.
- `catalog.match_qso` compares a directory with the references or DXCC codes recorded
  in the log, which is useful for “worked versus missing” questions.
- `qlog.get_context`, `qlog.get_capabilities`, and `qlog.get_schema` describe the
  selected database and its available fields.

Every QSO operation uses an explicit station scope: a callsign, a station profile, or
all QSOs. This matters when one QLog database contains several callsigns or operating
locations. The assistant should also distinguish a recorded confirmation from an award
credit granted by ARRL, RAC, IOTA, POTA, or another organization.

## Codex configuration

After cloning the repository and running `uv sync`, register the server with
[Codex](https://developers.openai.com/codex/mcp/) using its absolute paths:

```bash
codex mcp add qlog -- uv run \
  --directory /absolute/path/to/QLog-MCP \
  --frozen qlog-mcp \
  --database /absolute/path/to/qlog.db \
  --usage-log /absolute/path/to/qlog-mcp-usage.jsonl
```

Replace the example paths with the repository, QLog database, and desired log
locations on your computer. Check the saved configuration with `codex mcp list`,
then start a new Codex session and use `/mcp` to verify that `qlog` is connected.

## AppImage

Every push to `main` or a `v*` tag builds self-contained `x86_64` and `aarch64`
AppImages in GitHub Actions. The filename contains the version derived from Git.
Download the artifact for your architecture, make the AppImage executable, and
register its absolute path directly with the MCP client:

```bash
chmod +x qlog-mcp-0.2.3+gabcdef-x86_64.AppImage
codex mcp add qlog -- /absolute/path/to/qlog-mcp-0.2.3+gabcdef-x86_64.AppImage \
  --database /absolute/path/to/qlog.db \
  --usage-log /absolute/path/to/qlog-mcp-usage.jsonl
```

The AppImage contains its own Python 3.12 and the dependencies pinned by
`uv.lock`; no system Python or cloned source tree is needed. Check the embedded
package version with `qlog-mcp-*.AppImage --version`.

An exact `v0.2.0` tag produces version `0.2.0`. Each following commit advances
the patch component by one, so the third commit produces `0.2.3+gabcdef`.
Uncommitted tracked changes add `.dirty`. Before the first version tag, builds
use `0.1.0+g<commit>` as a bootstrap version.

Pushing a version tag creates a GitHub Release after both AppImages have been
built. Its notes contain the abbreviated Git log since the previous tag, and
the release includes both AppImages and their SHA-256 files:

```bash
git tag v0.2.0
git push origin v0.2.0
```

Build locally on Linux with `uv`:

```bash
packaging/appimage/build-appimage.sh
```

## Development

Install [uv](https://docs.astral.sh/uv/) and run:

```bash
uv sync
uv run qlog-mcp
```

The server uses the MCP `stdio` transport by default. An explicit database path
can be supplied without opening the database during server startup:

```bash
uv run qlog-mcp --database /path/to/qlog.sqlite
```

The path lookup order is:

1. `--database`
2. `QLOG_DB_PATH`
3. the documented `qlog-service.json` discovery file

## Usage logging

`--usage-log` enables a rotating JSON Lines log for investigating how an LLM
uses the server. Each tool call records its duration, a redacted argument
summary, result size, and the generated SQL with placeholders. SQL execution
time, parameter count and types, and returned row count are recorded separately.
Filter values and QSO content are never written. The log rotates at 10 MiB and
keeps five backups. Omit `--usage-log` to disable it.

Show calls that were slow, large, paginated, or truncated:

```bash
jq -c 'select(.flags | length > 0)' /path/to/qlog-mcp-usage.jsonl
```

See [docs/architecture.md](docs/architecture.md),
[docs/tools.md](docs/tools.md),
[docs/analysis-examples.md](docs/analysis-examples.md), and
[docs/qlog-discovery.md](docs/qlog-discovery.md) for the current contract. The
database schema support policy is recorded in
[docs/compatibility.md](docs/compatibility.md).

Example query arguments:

```json
{
  "scope": {"station_scope": "all", "date_from": "2026-01-01"},
  "filters": {
    "logic": "and",
    "conditions": [
      {"field": "band", "op": "in", "value": ["15m", "20m"]},
      {"field": "mode", "op": "in", "value": ["CW", "FT8"]}
    ]
  },
  "fields": ["datetime", "callsign", "band", "mode", "dxcc"],
  "sort": [{"field": "datetime", "direction": "desc"}],
  "limit": 100
}
```

Discover catalog fields with `qlog.get_schema(domain="catalog")`, then query a directory
without using its physical table or column names:

```json
{
  "catalog": "sota",
  "filters": {
    "conditions": [{"field": "association", "op": "eq", "value": "Czech Republic"}]
  },
  "fields": ["reference", "name", "points", "valid_to"],
  "sort": [{"field": "points", "direction": "desc"}],
  "limit": 25
}
```

After an LLM has obtained accepted confirmation states from external DXCC rules, it can
request the corresponding missing set without downloading individual QSOs:

```json
{
  "catalog": "dxcc",
  "qso_field": "dxcc",
  "scope": {"station_scope": "all"},
  "qso_filters": {
    "logic": "or",
    "conditions": [
      {"field": "lotw_received", "op": "eq", "value": "Y"},
      {"field": "qsl_received", "op": "eq", "value": "Y"}
    ]
  },
  "catalog_filters": {
    "conditions": [{"field": "deleted", "op": "eq", "value": false}]
  },
  "relation": "not_matched",
  "fields": ["code", "name", "continent"]
}
```

## Checks

```bash
uv run pytest
uv run ruff check .
```
