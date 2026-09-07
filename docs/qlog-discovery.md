# QLog database discovery

The server can be pointed at a database explicitly, configured through the environment,
or allowed to follow the discovery document written by QLog. The precedence is fixed so
that a temporary diagnostic database cannot silently override an explicit choice:

1. `qlog-mcp --database PATH`
2. `QLOG_DB_PATH`
3. the first readable QLog `qlog-service.json` location containing a database path

An explicit CLI path wins even when a discovery file exists. An unset lower-priority
source is skipped. The path itself is checked when a data operation opens SQLite; if no
source provides a path, the server remains constructible but QSO operations report that
no database was configured.

## Platform locations

| Platform | Discovery file |
| --- | --- |
| Linux and other Unix-like systems | `$XDG_CONFIG_HOME/qlog/qlog-service.json`, or `~/.config/qlog/qlog-service.json` |
| Windows | `%LOCALAPPDATA%\\QLog\\qlog-service.json` |
| macOS | `~/Library/Application Support/QLog/qlog-service.json` |

The implementation deliberately uses these documented locations instead of reproducing
QLog's Qt `QStandardPaths` behavior. QLog remains the owner of the database location;
the MCP server only consumes the published path.

## Accepted discovery formats

QLog MCP accepts the current structured form:

```json
{
  "protocol": 1,
  "database": {
    "path": "/path/to/qlog.sqlite",
    "schema": 42
  },
  "qlog_version": "0.52.0"
}
```

It also accepts the transitional compact form:

```json
{"database": "/path/to/qlog.sqlite"}
```

Only the database path is needed by the server. The published protocol, schema, and
version metadata are retained by QLog for consumers that need them, but they do not
override the runtime capability check performed against the opened database.

## Operational behavior

Discovery is lazy. Creating the MCP server, listing tools, and asking for server
capabilities do not open the SQLite file. The file is opened only when a QSO or catalog
operation needs data, and then in SQLite read-only mode with `PRAGMA query_only = ON`.

For a predictable deployment, pass an absolute `--database` path. For a normal desktop
QLog installation, leave the path unset and let QLog publish `qlog-service.json`. If a
request fails with “database not configured,” inspect the three sources above in order;
the server does not create a database or repair a malformed discovery document.
