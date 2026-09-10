# Compatibility

QLog MCP negotiates compatibility from the database that is actually opened. It does
not assume that a particular QLog application version implies a fixed set of columns.
This matters for long-lived logs: a newer server can still serve an older database,
while fields introduced later should disappear cleanly instead of producing misleading
empty results.

## What is required

The core QSO path needs the `contacts` table with at least `id`, `start_time`, and
`callsign`. The current package line is intentionally described broadly:

| QLog MCP | Database requirement |
| --- | --- |
| 0.1.x | Adaptive `contacts` schema; core access requires `id`, `start_time`, and `callsign` |

The database is inspected lazily. A missing database, unreadable file, or missing core
schema is an opening/configuration failure; an optional field that is absent is a
capability difference.

## Capability rules

`qlog.get_schema` is the authoritative capability snapshot for the current connection.
It lists only semantic fields backed by the selected database and publishes each field's
operators, aggregate functions, cardinality, and any catalog mapping.

| Feature | Additional capability | Behavior when absent |
| --- | --- | --- |
| Default QSO fields | Columns in `contacts` | The default projection is reduced where possible |
| Explicit projection/filter/sort/group/metric | The requested source column(s) | The request fails with a clear compatibility error |
| `grid4`, `my_grid4` | `gridsquare`, `my_gridsquare` | Each derived field disappears independently |
| Profile scope | `station_profiles(profile_name, callsign, locator)` plus matching station fields | Profile scope is unavailable; callsign scope can still work |
| `base_callsign`, Wavelog fields | Matching columns in `contacts_autovalue` | Fields are omitted or return `null` when no matching row exists |
| Frequency-based band fallback | `bands(name, start_freq, end_freq)` | Stored `band` values work; frequency-only QSOs cannot be assigned a band |

Optional-column support is per field. A missing optional column does not disable the
whole QSO API, and a QSO without a corresponding `contacts_autovalue` row does not make
the rest of that QSO unavailable.

## Catalog compatibility

Catalog support is discovered independently from QSO support. The initial semantic
catalog families are POTA, SOTA, WWFF, IOTA, DXCC, and satellite. A catalog is advertised only when
its recognizable source is available; missing optional columns remove only the affected
catalog fields and may change `source_capability` from `full` to `reduced`.

`qlog.get_schema(domain="catalog")` also publishes the only legal mappings for
`catalog.match_qso`:

| Catalog | Contacted/logging QSO fields |
| --- | --- |
| POTA | `pota_ref`, `my_pota_ref` |
| SOTA | `sota_ref`, `my_sota_ref` |
| WWFF | `wwff_ref`, `my_wwff_ref` |
| IOTA | `iota`, `my_iota` |
| DXCC | `dxcc`, `my_dxcc` |
| Satellite | `satellite_name` |

DXCC prefers QLog's complete directory capability, including deletion and validity
information. If that source is unavailable, the server uses a simpler reduced directory;
it never merges rows from two sources. Catalog dates are normalized to ISO
`YYYY-MM-DD`; blank, invalid, and `0000-00-00` sentinel values become `null`.
The satellite catalog requires only a recognizable satellite-name column; its other directory
fields are independently optional and disappear from a reduced capability.

## Error categories and client workflow

These cases have different meanings:

- **Configuration/opening error:** no database was selected, the file does not exist, or
  the required core schema cannot be opened.
- **Compatibility error:** a known semantic field or mapping is not available in this
  database.
- **Validation error:** the name does not belong to the selected semantic registry, the
  operator is not allowed for that field, or the request shape is invalid.

A robust client should therefore:

1. call `qlog.get_context` and choose the station scope;
2. call `qlog.get_schema` once for each needed domain;
3. build requests only from advertised fields and operations;
4. treat a missing field as “not available in this database,” not as evidence that the
   stored value is empty;
5. refresh the snapshot after a server/database change or a compatibility error.
