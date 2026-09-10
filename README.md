# QLog MCP

QLog MCP is a small, read-only bridge between an AI assistant, such as Codex or Claude,
and [QLog](https://github.com/foldynl/QLog) database. The AI assistant uses a language model (LLM) to understand and answer your questions.

QLog MCP is **not** an AI agent and has no chat interface of its own. The AI assistant holds the
conversation and reasons about your question. QLog MCP only gives it safe,
well-described access to the QSOs and reference data stored by QLog.

```text
You → AI assistant → QLog MCP → QLog database
```

The server never changes, repairs, migrates, or creates a QLog database.

## What can it help with?

Ask your AI assistant ordinary questions about your logbook, for example:

- “Which DXCC entities did I work last year, and which have LoTW confirmation?”
- “How did my CW activity change over the past six months?”
- “Show my most recent QSO with each DXCC on 6 m.”
- “Which POTA references in my log are absent from the current directory?”

For summaries and trends, the server calculates the result in SQLite instead of sending
every QSO to the assistant.

Award and contest rules still belong to the AI assistant and the relevant award programme.
QLog MCP reports what is recorded in the log; it does not declare an official award,
score, or confirmation credit.

## Connect it to Codex

Choose one installation method, then start a new Codex session and use `/mcp` to check
that `qlog` is connected.

### AppImage

Download the AppImage for your architecture from
[GitHub Releases](https://github.com/foldynl/QLog-MCP/releases), install it under a stable name, and register that stable path:

```bash
install -Dm755 qlog-mcp-0.2.0-x86_64.AppImage "$HOME/.local/bin/qlog-mcp.AppImage"
codex mcp add qlog -- "$HOME/.local/bin/qlog-mcp.AppImage" \
  --database /absolute/path/to/qlog.db
```

Keep the registered path unchanged. To update the server later, install the new AppImage
to the same `$HOME/.local/bin/qlog-mcp.AppImage` path and start a new Codex session. This
avoids changing the MCP configuration for every release.

### From Source Code

Clone this repository, install its dependencies, and register the server:

```bash
git clone https://github.com/foldynl/QLog-MCP.git
cd QLog-MCP
uv sync
codex mcp add qlog -- uv run \
  --directory /absolute/path/to/QLog-MCP \
  --frozen qlog-mcp \
  --database /absolute/path/to/qlog.db
```

`--database` is optional when QLog's discovery file is available. See [database discovery](docs/qlog-discovery.md) for the lookup order and supported paths.

## Optional troubleshooting log

Add `--usage-log /path/to/qlog-mcp-usage.jsonl` to either command to record private,
rotating diagnostics. It records tool names, timings, result sizes, and SQL with
placeholders. It never records QSO content, callsigns from scopes, or filter values.

## Learn more

| Need | Read |
| --- | --- |
| Available tools, fields, filters, and limits | [Tool reference](docs/tools.md) |
| Award, contest, and activity-analysis examples | [Analysis examples](docs/analysis-examples.md) |
| Database discovery and compatibility | [Discovery](docs/qlog-discovery.md) and [compatibility](docs/compatibility.md) |
| Design, privacy, and read-only guarantees | [Architecture](docs/architecture.md) |
| AppImage builds, versions, and GitHub releases | [Release guide](docs/releasing.md) |
