# Public tools

This page is the precise tool contract. For the user-oriented view, start with the
[README](../README.md); for composed award and contest analyses, see
[analysis examples](analysis-examples.md).

## Which tool answers which question?

| Need | Tool | Why |
| --- | --- | --- |
| Choose a callsign/profile and see the log bounds | `qlog.get_context` | Returns dates, station callsign/grid pairs, operators, and profiles |
| Learn what this server/database can support | `qlog.get_capabilities`, `qlog.get_schema` | Separates server features from fields available in this database |
| Inspect one or a small page of actual QSOs | `qso.query` | Returns selected semantic fields with filters, sorting, and pagination |
| Count or compare many QSOs | `qso.aggregate` | Calculates metrics in SQLite and returns aggregate rows only |
| Look up a directory entry | `catalog.query` | Returns directory facts, not award decisions |
| Find catalog entries present or absent in the log | `catalog.match_qso` | Compares distinct semantic keys without returning QSO content |
| Compare a downloaded club roster with QSOs | `membership.match_qso` | Uses each QSO date |

The eight public tools are:

- `qlog.get_context`
- `qlog.get_capabilities`
- `qlog.get_schema`
- `catalog.query`
- `catalog.match_qso`
- `membership.match_qso`
- `qso.aggregate`
- `qso.query`

## The contract in one minute

Every QSO operation requires an explicit station scope. A safe client first calls
`qlog.get_context`, asks the user to choose a callsign/profile or all QSOs, then reuses
that choice. It calls `qlog.get_schema` once per needed domain, constructs requests only
from the advertised semantic fields and operators, and refreshes the snapshot after a
database/server change or compatibility error.

Use `qso.aggregate` by default for statistics. Use `qso.query` when the user needs the
actual contact rows, a last/first record, or evidence for a summary. Catalog results are
reference facts. They do not mean “worked”, “confirmed”, “needed”, “valid”, or “award
credit” until the caller applies the relevant external rules.

`qlog.get_schema` accepts `qso` or `catalog` as its domain. It describes semantic
fields and operations without exposing the physical SQLite schema. Call each needed domain
once per server connection and reuse the result. Refresh it only after the server or database
changes, or when a compatibility error indicates that the stored capability snapshot may be
stale.

## Reference catalogs

`qlog.get_schema(domain="catalog")` discovers which of the semantic `pota`, `sota`,
`wwff`, `iota`, `dxcc`, `satellite`, `membership`, and `membership_clubs` catalogs are
available in the selected database. Each
available catalog publishes its fields, types, per-field operators, default projection,
deterministic default order, page limit, source capability, and QSO fields that can later
be matched to its references. A missing catalog is omitted; missing optional columns remove
only their semantic fields. `source_capability` is `full` or `reduced` and never reveals a
physical table name.

`catalog.query` uses the same nested filter shape and scalar operators as QSO queries. It
supports semantic projection and sorting plus `limit`/`offset` pagination with
`has_more` and `next_offset`. Text comparisons are case-insensitive, filter values are
bound parameters, and a catalog's reference or numeric code is appended as a stable
tie-breaker when needed.

For example, this returns currently dated Czech SOTA summits ordered by points. The
server returns directory facts; the LLM remains responsible for interpreting any award,
activity, or contest rules:

```json
{
  "catalog": "sota",
  "filters": {
    "conditions": [
      {"field": "association", "op": "eq", "value": "Czech Republic"},
      {"field": "valid_from", "op": "lte", "value": "2026-09-09"},
      {"field": "valid_to", "op": "gte", "value": "2026-09-09"}
    ]
  },
  "fields": ["reference", "name", "points", "valid_from", "valid_to"],
  "sort": [{"field": "points", "direction": "desc"}],
  "limit": 100
}
```

Catalog dates are returned as ISO `YYYY-MM-DD` values. Blank dates, invalid dates, and
`0000-00-00` sentinels become `null`; source values in `DD/MM/YYYY` and ISO timestamp form
are normalized. DXCC uses QLog's source with deleted-entity and validity information when
that complete source is present. It otherwise falls back to the simpler directory and
advertises a reduced capability; rows from the two sources are never merged.

`catalog.match_qso` compares the catalog key with distinct values from one compatible
QSO field. It applies the required station `scope` and `qso_filters` to the QSO set, and
applies `catalog_filters` independently to the catalog set. Use only a `qso_field` listed
in the catalog's `compatible_qso_fields`:

| Catalog | Compatible QSO fields |
| --- | --- |
| `pota` | `pota_ref`, `my_pota_ref` |
| `sota` | `sota_ref`, `my_sota_ref` |
| `wwff` | `wwff_ref`, `my_wwff_ref` |
| `iota` | `iota`, `my_iota` |
| `dxcc` | `dxcc`, `my_dxcc` |
| `satellite` | `satellite_name` |

`membership` and `membership_clubs` deliberately have no `catalog.match_qso` mapping.
They contain only membership lists the user downloaded into QLog, not every club in the world.
Before analyzing a named club, search `membership_clubs`; if the club is absent, the server has
no data for it and absence must not be treated as non-membership. Membership needs a QSO date
and club-specific policy. Use `membership.match_qso` for the time-aware roster complement, the
QSO membership fields below for QSO evidence, and apply the award rule outside the server.

The relation names describe set membership only:

- `matched` returns catalog keys occurring in the filtered QSO set;
- `not_matched` returns catalog keys absent from that QSO set;
- `qso_only` returns non-empty QSO keys absent from the filtered catalog.

For `matched` and `not_matched`, `fields` and `sort` use catalog semantic fields. For
`qso_only`, the result always contains `key` and `qso_count`, and sorting accepts only
those two names. `qso_count` counts QSOs containing the key; a duplicate list item within
one QSO counts once. List-valued QSO fields use the same normalized item semantics as
`qso.aggregate` explosion. Null, empty, and whitespace-only keys are ignored, and text
keys compare case-insensitively.

Every response includes complete counts for `catalog_values`, `matched_values`,
`not_matched_values`, and `qso_only_values`, even when `items` are paginated. Detail rows
use deterministic `limit`/`offset` pagination. These four summary values count distinct
non-empty keys, not QSO rows. Only `qso_count` in a `qso_only` item counts QSO occurrences.
`qso_only` deliberately returns no QSO content; use `qso.query` with the returned key and
`one_per_group` when evidence is needed.

For example, after the LLM has selected acceptable confirmation states from external
DXCC rules, it can ask for the filtered catalog complement without transferring QSOs:

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
  "fields": ["code", "name", "prefix", "continent"],
  "sort": [{"field": "name", "direction": "asc"}]
}
```

The server does not decide that `matched` means worked or confirmed, or that
`not_matched` means needed for an award. See [analysis examples](analysis-examples.md) for
compositions in which the LLM supplies external rules.

The initial semantic fields are:

- POTA: `reference`, `name`, `active`, `dxcc`, `location`, `latitude`, `longitude`, `grid`;
- SOTA: `reference`, `association`, `region`, `name`, `altitude_m`, `altitude_ft`,
  `longitude`, `latitude`, `points`, `bonus_points`, `valid_from`, `valid_to`;
- WWFF: `reference`, `status`, `name`, `program`, `dxcc`, `state`, `county`, `continent`,
  `iota`, `grid`, `latitude`, `longitude`, `iucn_category`, `valid_from`, `valid_to`;
- IOTA: `reference`, `name`;
- DXCC: `code`, `name`, `prefix`, `deleted`, `continent`, `cq_zone`, `itu_zone`,
  `latitude`, `longitude`, `valid_from`, `valid_to`.
- Satellite: `name`, `number`, `uplink`, `downlink`, `beacon`, `mode`, `callsign`, `status`.
- Membership: `club`, `callsign`, `member_id`, `valid_from`, `valid_to`,
  `valid_from_state`, `valid_to_state`.
- Membership clubs: `club`, `name`, `source_file`, `source_updated`, `member_count`.

The satellite catalog is QLog's stored directory snapshot. Its `name` is the only
comparison key and can be matched only with QSO `satellite_name`. `number`, frequency-like
text, mode, callsign, and status are directory facts, not parsed operating parameters or
evidence that a QSO used a satellite.

The membership catalog contains only member base callsigns from lists downloaded into QLog. Its
compact `YYYYMMDD` boundaries are returned as ISO dates; blank boundaries are open and malformed
non-empty values become `null`. `membership_clubs` contains metadata for those downloaded lists.
If a club is not present there, the server has no membership data for it; this does not mean a
callsign is not a member. Neither catalog determines a diploma credit or resolves club-specific
list rules. `valid_*_state` distinguishes blank open boundaries from malformed non-empty data.

## Membership roster/QSO matching

`membership.match_qso` requires an exact club from `membership_clubs` and an explicit QSO
scope. It returns one row per member base callsign, with QSO count and first/last matching QSO.
`membership_basis=qso_date` requires the member record to cover the date of each QSO; empty
dates are open and malformed non-empty dates are excluded. `directory_snapshot` instead compares
every callsign in QLog's locally stored club roster with any scoped QSO and intentionally ignores
membership dates. Use `worked` or `not_worked` for either basis.

`member_as_of` optionally limits the roster to records valid on that date. It never replaces the
per-QSO date test. A missing club list is an error, not an empty roster or evidence of
non-membership.

The complete `summary` counts the unique roster (`member_callsigns`), its worked and not-worked
callsigns, and distinct matching QSOs; it is unaffected by pagination. It always reports
`invalid_membership_records`; `excluded_invalid_membership_records` is nonzero only for
`qso_date`.

## QSO data

The QSO schema describes semantic fields such as `callsign`, `dxcc`, `band`, `distance`,
or `pota_ref`.

### Field choices that affect the answer

Several pairs look similar but answer different questions:

| Question | Use | Not |
| --- | --- | --- |
| Which station did I contact? | `callsign` | `station_callsign` (your logging identity) |
| Which callsign did I log from? | `station_callsign` | `callsign` |
| Which operator-facing mode was used? | `mode` | `adif_mode` when an FT8/FT4 submode matters |
| Which ADIF parent mode was stored? | `adif_mode` | `mode` if the parent category is the question |
| Which whole amateur band? | `band` | `frequency` for approximate values such as 14 MHz |
| Which exact transmit frequency or range? | `frequency` | `band`, which intentionally includes frequency-only records |
| Which DXCC entity? | numeric `dxcc` | free-form `country` when stable entity identity matters |

The QSO schema marks contacted-side and logging-side fields with `side` and
`paired_field`. Use that metadata for award questions: `dxcc` and `callsign` describe the
other station, while `my_dxcc` and `station_callsign` describe the logging side.

Every advertised QSO field has `cardinality` equal to `one` or `many`. Structured list fields also publish an `item_type` and `list_semantics` describing exact matching and the value returned by explosion. Relevant station fields publish `side` (`contacted`, `logging`, `operator`, or `qso`) and, when the opposite field is
available, `paired_field`. This lets a client distinguish `pota_ref` from `my_pota_ref`, or `callsign` from `station_callsign`, without knowing QLog's physical column names.

The list fields currently advertised with `cardinality=many` are:

- `pota_ref` and `my_pota_ref`: comma-delimited POTA references;
- `vucc_grids` and `my_vucc_grids`: comma-delimited Maidenhead grids;
- `usaca_counties` and `my_usaca_counties`: colon-delimited `STATE,County` items;
- `county_alt` and `my_county_alt`: semicolon-delimited alternate subdivision
  entries, which may contain slash-delimited localities;
- `credit_submitted` and `credit_granted`: comma-delimited ADIF credits with
  optional colon and ampersand-delimited QSL media;
- `award_submitted` and `award_granted`: comma-delimited sponsored awards.
- `member_clubs_at_qso_date`: club identifiers for the contacted base callsign from lists the
  user downloaded into QLog, whose stored membership interval includes the QSO date; an empty
  start or end is unbounded, while a malformed non-empty boundary excludes that record;
- `member_clubs_in_directory`: club identifiers for the contacted base callsign from every
  stored record in the downloaded lists, without testing membership dates or current validity.

These fields return a string representation in query results. Membership club values are derived
and comma-delimited; the other fields retain their stored representation. Their additional `has`,
`has_any`, and `has_all` operators compare complete semantic items case-insensitively. They ignore
outer whitespace and whitespace around separators defined by the field's list syntax. Empty
requested lists are invalid, and null, empty, or whitespace-only stored values contain no items.
For example, this finds a QSO containing both parks even if the stored list uses different case or
includes an optional location on the first reference:

```json
{
  "conditions": [
    {
      "field": "pota_ref",
      "op": "has_all",
      "value": ["K-4562", "K-0001"]
    }
  ]
}
```

An unqualified POTA item such as `K-4562` matches `K-4562@US-CA`; a qualified item requires that location. A bare credit such as `DXCC` matches the credit with any recorded media, while `DXCC:LOTW` also matches
`DXCC:LOTW&CARD`. To require both media, use `has_all` with `["DXCC:LOTW", "DXCC:CARD"]`; one requested item has the form `CREDIT` or `CREDIT:MEDIUM`:

```json
{"conditions":[{"field":"credit_granted","op":"has","value":"DXCC:LOTW"}]}
```

Malformed safely delimited items are kept as trimmed exact items instead of being dropped or guessed. Use the ordinary raw-string `contains` operator only as a deliberate diagnostic fallback for inconsistent legacy data; `has` never silently becomes a substring search.

`qso.query` is implemented. It supports field projection, nested `and`/`or` groups with optional `negate`, sorting, station/date/operator scope, and offset pagination. All filter values are bound SQLite parameters; clients cannot supply table names, column names, or SQL expressions.

Every QSO operation requires `scope.station_scope`. Its value is `callsign`, `profile`, or `all`. For `callsign`, pass one or more entries in `station_callsigns`; each entry contains a callsign and may contain a grid. For `profile`, pass one or more `station_profile_names`. `qlog.get_context` returns the available callsign/grid pairs and station profiles.

`qlog.get_schema` publishes the supported `operators` separately for every semantic field, and `filter_operator_semantics` describes each operator. Text fields additionally support `contains`, `starts_with`, and `ends_with`; text matching is case-insensitive. `between` includes both boundaries. `is_null` and `is_not_null` check SQL `NULL` specifically. `is_empty` treats `NULL`, an empty
string, and a string containing only whitespace as empty; `is_not_empty` is its inverse. `limit` is restricted to 1–1000 and the response reports `has_more` plus `next_offset`.

`one_per_group` can keep the `first` or `last` matching QSO for every combination of its fields. Scope and filters are applied first; selection uses QSO datetime and contact ID as a deterministic tie-breaker. The ordinary `sort`, `limit`, and `offset` are then applied to the selected complete QSO rows. A list-valued field groups by its complete stored string here; per-item explosion belongs to `qso.aggregate`.

Use the semantic `band` field for a whole amateur band such as `20m`, including an approximate frequency reference such as “14 MHz”. A missing band is derived from the stored frequency and QLog's `bands` table, so band operations include band-only, frequency-only, and combined records. `qlog.get_schema` publishes the available band names and MHz ranges.

Use `frequency` for an exact transmit frequency such as `14.145` MHz or for a subrange. A band-only QSO cannot match because it has no exact frequency. The same distinction applies to `band_rx` and `frequency_rx`.

The QSO schema also publishes technical fields derived from the logged ADIF values. They can be selected, filtered, sorted, grouped, and aggregated like stored fields:

- `grid4` and `my_grid4` return the uppercase four-character Maidenhead square
  for the contacted and logging station. They are null unless the corresponding
  stored locator is valid and has 4, 6, or 8 characters.
- `is_split` is true when exact RX and TX frequencies differ by at least 1 Hz,
  or when the resolved RX and TX bands differ. It is false when the log contains
  no evidence of split operation; it does not infer unrecorded radio state.
- `is_cross_band` is true only when both resolved bands are known and differ.
  Band resolution uses stored `BAND`/`BAND_RX` first and exact-frequency lookup
  second, so it also recognizes frequency-only cross-band records.
- `frequency_offset_khz` is the signed value `(FREQ_RX - FREQ) * 1000` in kHz.
  A positive value means RX above TX and a negative value means RX below TX. It
  is null unless both exact frequencies are logged; bands alone cannot provide
  an exact offset.
- `duration_seconds` is UTC end time minus UTC start time in seconds. It is null
  for a missing or invalid timestamp and when the recorded end precedes start.
- `rst_sent_numeric` and `rst_received_numeric` expose a report only when its
  complete text is a signed or unsigned integer. Values such as `5NN` become
  null instead of being partly or incorrectly converted. Use these fields for
  numeric statistics only after restricting QSOs to compatible modes or report
  conventions: phone `59`, CW `599`, and digital `-12` are different scales.

`qso.aggregate` calculates statistics in SQLite and returns aggregate rows, not individual QSOs. It uses the same required `scope` and optional nested `filters` as `qso.query`. Every semantic field available in `qlog.get_schema` may be used in `group_by`, including fields that commonly have many distinct values. The
response is therefore limited to 100 groups by default and 1000 at most, and reports `truncated` when more groups matched. Aggregate results are not offset paginated.

Aggregation applies operations in this order: scope and top-level filters, requested list expansion, `one_per_group`, grouping and metrics, `having`, then ordering and limit. `one_per_group` uses the same deterministic QSO datetime and contact-ID ordering as `qso.query`. When its key names a list field also exploded by `group_by`, the individual exploded item is used in that key. Metric-local filters see only the retained rows.

For example, after an LLM reads external contest rules and selects the appropriate duplicate key, it can count the retained rows without transferring individual QSOs:

```json
{
  "one_per_group": {
    "fields": ["callsign", "band", "mode"],
    "keep": "first"
  },
  "group_by": ["band"],
  "metrics": [{"function": "count", "as": "deduplicated_qsos"}]
}
```

The result is a neutral count for the caller-supplied key, not an official duplicate or score decision.

The additional time dimensions are derived from the UTC QSO start time:

- `year`: `YYYY`
- `month`: `YYYY-MM`
- `day`: `YYYY-MM-DD`
- `hour`: 0 through 23
- `weekday`: ISO weekday 1 (Monday) through 7 (Sunday)

`group_by` also accepts bucket objects. Set `interval` to `minute`, `hour`, `day`, `week`, `month`, `quarter`, or `year` for a date/datetime field; `minute` and `hour` require a datetime. A minute bucket also requires integer `size` from 1 through 60 and creates fixed-size UTC intervals aligned to the Unix epoch. Weekly buckets return the Monday date and hourly buckets return a UTC timestamp. `qlog.get_schema` publishes every
exact returned format under `interval_semantics`. Set `bucket_size` for a numeric field; its result is the
lower bucket boundary. Each group object requires an `as` output name and exactly one of `interval`, `bucket_size`, or `explode=true`.

`explode=true` groups by individual items of a list-valued field. Duplicate equal items in one QSO contribute once. POTA explosion returns the park without its optional `@` location, CreditList explosion returns the credit name, and alternate subdivisions return one `enumeration-name:locality` per locality.
Exploded identities use uppercase for stable case-insensitive grouping. Malformed items remain visible as their trimmed value. For example:

```json
{"field":"pota_ref","explode":true,"as":"park"}
```

A plain string list field in `group_by`, such as `"pota_ref"`, continues to group by the complete stored string. Use the object form above when the question is about individual list items.

Metrics support `count`, `distinct_count`, `sum`, `avg`, `min`, and `max`. `count` has no field and counts QSO rows. Other functions require a semantic field. `distinct_count` is available for every field and ignores SQL `NULL`, empty strings, and whitespace-only values. `sum` and `avg` are available for numeric fields; `min` and `max` are available for numeric, date, and datetime fields. `qlog.get_schema` publishes the supported `aggregate_functions` for every available field.

`distinct_count` may use `fields` with 1 to 10 scalar semantic fields to count composite units as typed tuples. It excludes a QSO when any tuple component is null or empty and compares text components case-insensitively. For example, after an LLM selects `dxcc` plus `band` as the unit required by an external analysis:

```json
{"function":"distinct_count","fields":["dxcc","band"],"as":"band_entities"}
```

Use exactly one of singular `field` or composite `fields`. List fields are intentionally rejected in `fields`; explode a list field as a group dimension so the item relationship remains explicit.

For `distinct_count` only, set `explode=true` to count distinct semantic items inside a list-valued field instead of distinct complete stored strings:

```json
{"function":"distinct_count","field":"pota_ref","explode":true,"as":"parks"}
```

`having` filters completed aggregate groups by metric alias. It supports `eq`, `neq`, `gt`, `gte`, `lt`, and `lte`; multiple conditions are combined with `and`. Values are passed to SQLite as bound parameters.

Missing group values (`NULL`, empty strings, or whitespace-only strings) are returned in one `null` group. Text grouping and distinct counting are case-insensitive, matching the existing filter behavior.

Each metric requires an `as` name used in returned rows and may have its own optional `filters`. Metric filters are combined with the query scope and top-level filters. This permits conditional metrics such as all QSOs and European QSOs in the same yearly groups. `order_by` may refer to a group dimension or metric alias. Without `order_by`, results are ordered by the first metric descending and then by group dimensions ascending.

The semantic `mode` field follows QLog's operator-facing display: it returns `submode` when present and otherwise `mode`. Thus an FT8 record stored as `mode=MFSK, submode=FT8` matches `mode = FT8`. The physical ADIF category remains available as `adif_mode`, and `submode` can also be requested directly.

The semantic `base_callsign` field is QLog's computed contacted callsign without portable prefixes or suffixes. `wavelog_upload_status` and `wavelog_upload_date` expose the per-QSO Wavelog integration state. These fields are available when the selected database contains `contacts_autovalue`.

All `*_upload_status` fields use the [ADIF QSO Upload Status](https://www.adif.org/317/ADIF_317.htm#QSOUploadStatus_Enumeration) enumeration. `Y` means uploaded and accepted by the service, `N` means the QSO must not be uploaded, and `M` means it was modified after an earlier upload. `qlog.get_schema` exposes these values and meanings with every available upload
status field.

`scope.station_profile_names` follows QLog's own profile matching concept. QLog stores profile names as the primary key; QSO rows do not contain a profile ID.
