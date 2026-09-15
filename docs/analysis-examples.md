# Composing QSO and catalog analysis

QLog MCP deliberately separates **recorded facts** from **domain rules**. The server can
tell an assistant which QSOs are in a selected scope, which DXCC code or reference is
stored, how many distinct values exist, and which catalog rows match. An external award
or contest rulebook must still say whether a confirmation is acceptable, whether an entity
was current on a given date, which duplicate key applies, or how a score is calculated.

That separation makes the results auditable: the request defines the population, the
server returns the evidence, and the assistant explains the rule-based conclusion and
its assumptions.

## A repeatable analysis workflow

For most questions, use this sequence:

1. Call `qlog.get_context` and choose one station callsign, a profile, or explicitly all
   QSOs. Never silently broaden a callsign question to the whole database.
2. Call `qlog.get_schema` for the QSO or catalog domain and select only advertised fields,
   operators, mappings, and aggregate functions.
3. Use `qso.aggregate` for counts, trends, distinct entities, and other summaries. Use
   `qso.compare_sets` for keys present in one or both of two QSO populations. Use
   `qso.query` only when the actual QSO rows are needed as evidence.
4. Use `catalog.query` to enrich known references with directory names and metadata, or
   `catalog.match_qso` to calculate matched, missing, and QSO-only sets without moving
   individual QSOs to the client.
5. State the external rule, date window, confirmation definition, duplicate policy, and
   any limitations in the answer.

The examples below are building blocks, not ready-made award calculators.

## DXCC worked on 20 m but never on 15 m

This is a direct comparison of two QSO populations with the same semantic key. It needs
one server-side set operation rather than two aggregations and client-side pagination:

```json
{
  "left": {
    "scope": {"station_scope": "all"},
    "key": "dxcc",
    "filters": {"conditions": [{"field": "band", "op": "eq", "value": "20m"}]}
  },
  "right": {
    "scope": {"station_scope": "all"},
    "key": "dxcc",
    "filters": {"conditions": [{"field": "band", "op": "eq", "value": "15m"}]}
  },
  "relation": "left_only"
}
```

Each result includes the QSO count and first/last QSO on both sides. The relation says
only that a DXCC key occurs in one filtered population and not the other; it does not
interpret award status.

## DXCC entities absent from selected confirmations

After external DXCC rules tell the LLM which confirmation states and entity dates to use,
one `catalog.match_qso` call returns the catalog complement and complete set counts:

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
  "fields": ["code", "name", "prefix", "continent"]
}
```

The returned rows are absent from the caller-selected QSO population. The server does not
claim that they are officially needed or creditable.

For a “worked” list, remove the confirmation filter. For a “confirmed” list, choose the
states accepted by the relevant award rules first. `lotw_received = Y` and
`qsl_received = Y` are recorded ADIF statuses, not proof that ARRL or another award
manager has already granted credit. If the question combines callsigns from different
operating DXCC entities, keep those scopes separate whenever the external award requires
contacts to come from one entity.

## POTA contacted and logging-side references

POTA references may contain multiple comma-delimited items. The QSO schema declares
`pota_ref` and `my_pota_ref` as semantic counterparts, so one comparison can return parks
worked as a hunter but never recorded on the logging-station side:

```json
{
  "left": {"scope": {"station_scope": "all"}, "key": "pota_ref"},
  "right": {"scope": {"station_scope": "all"}, "key": "my_pota_ref"},
  "relation": "left_only"
}
```

Each comma-delimited park is normalized and compared separately. Use `catalog.query` with
the returned references when names and locations are needed. Any activation threshold
still comes from external POTA rules and needs `qso.aggregate`.

## Most frequently contacted SOTA summits with catalog facts

One `catalog.match_qso` call returns the filtered QSO statistics together with summit
metadata:

```json
{
  "catalog": "sota",
  "qso_field": "sota_ref",
  "scope": {"station_scope": "all"},
  "relation": "matched",
  "fields": [
    "reference",
    "name",
    "points",
    "qso_count",
    "first_qso",
    "last_qso"
  ],
  "sort": [{"field": "qso_count", "direction": "desc"}]
}
```

Use `sota_ref` for contacted activators' summits and `my_sota_ref` for the logging
station's own activations. The catalog points are directory facts; the LLM still applies
any external SOTA scoring or validity rules.

## WWFF references absent from a selected directory population

`qso_only` finds recorded references that do not exist in the chosen filtered catalog and
returns their QSO occurrence counts:

```json
{
  "catalog": "wwff",
  "qso_field": "wwff_ref",
  "scope": {"station_scope": "all"},
  "catalog_filters": {
    "conditions": [{"field": "status", "op": "eq", "value": "active"}]
  },
  "relation": "qso_only",
  "sort": [{"field": "qso_count", "direction": "desc"}]
}
```

This is useful for spotting spelling, legacy-directory, or status questions. It does not
declare a QSO invalid. To inspect one key, call `qso.query` with `wwff_ref = <key>` and
optionally use `one_per_group` to limit evidence.

## Satellite names absent from the stored directory

Filter the QSO population explicitly to satellite propagation, then compare the recorded
satellite names with QLog's stored satellite directory:

```json
{
  "catalog": "satellite",
  "qso_field": "satellite_name",
  "scope": {"station_scope": "all"},
  "qso_filters": {
    "conditions": [{"field": "propagation_mode", "op": "eq", "value": "SAT"}]
  },
  "relation": "qso_only",
  "sort": [{"field": "qso_count", "direction": "desc"}]
}
```

The result can identify a legacy or differently spelled recorded name, or an outdated local
directory. It does not declare the QSO invalid and does not infer that every QSO carrying a
satellite name was a satellite QSO; that is why the `propagation_mode` filter is explicit.

## Club-member QSOs under an explicit rule

For a club whose published rule accepts QLog's stored membership interval, filter the semantic
membership list and aggregate the QSOs. Empty membership boundaries are already treated as
unbounded by `member_clubs_at_qso_date`:

```json
{
  "scope": {"station_scope": "all"},
  "filters": {
    "conditions": [
      {"field": "member_clubs_at_qso_date", "op": "has", "value": "EXAMPLE-CLUB"}
    ]
  },
  "group_by": ["band", "mode"],
  "metrics": [{"function": "count", "as": "qsos"}],
  "order_by": [{"field": "qsos", "direction": "desc"}]
}
```

Use `member_clubs_in_directory` only when a question explicitly asks about the current local
membership snapshot rather than membership on the QSO date. These are evidence fields, not an
award engine: the assistant still applies each club's rules for dates, confirmations, and credit.

To find downloaded members of a club not worked during their recorded membership periods, use
the dedicated set operation. It verifies that the club list exists and excludes malformed dates:

```json
{
  "club": "EXAMPLE-CLUB",
  "scope": {"station_scope": "all"},
  "relation": "not_worked",
  "sort": [{"field": "callsign", "direction": "asc"}]
}
```

For “members in my currently stored list whom I have ever worked”, set
`"membership_basis": "directory_snapshot"`. This is a local QLog snapshot, not a live club
directory, and it intentionally ignores membership dates.

## IOTA group reference versus island ID

The IOTA catalog key is the group reference such as `EU-001`, so it is compatible with
`iota` and `my_iota`:

```json
{
  "catalog": "iota",
  "qso_field": "iota",
  "scope": {"station_scope": "all"},
  "relation": "matched",
  "fields": ["reference", "name"]
}
```

`iota_island_id` identifies a specific island and is intentionally rejected for this
catalog comparison. The schema's `compatible_qso_fields` prevents the LLM from silently
mixing the two identifier types.

## Activity trend without downloading the log

For a question such as “How did my activity change month by month?”, group the UTC QSO
start time and calculate several metrics in one response:

```json
{
  "scope": {"station_scope": "callsign", "station_callsigns": [{"callsign": "OK1MLG"}]},
  "group_by": [{"field": "datetime", "interval": "month", "as": "month"}],
  "metrics": [
    {"function": "count", "as": "qsos"},
    {"function": "distinct_count", "field": "callsign", "as": "stations"},
    {"function": "distinct_count", "field": "dxcc", "as": "entities"}
  ],
  "order_by": [{"field": "month", "direction": "asc"}]
}
```

The returned rows describe activity, not a rate normalized for partial months. If the
question compares calendar periods of different lengths, explain that limitation or use
fixed-size UTC buckets. Add `one_per_group` only when the analysis has an explicit
deduplication key; it is not a universal declaration that duplicate QSOs are invalid.

## Generic contest calculation

After external contest rules supply the duplicate key, categories, multiplier unit, and
time interval, one aggregate request can produce neutral building blocks:

```json
{
  "scope": {"station_scope": "all"},
  "filters": {
    "conditions": [{"field": "contest_id", "op": "eq", "value": "EXAMPLE-CONTEST"}]
  },
  "one_per_group": {
    "fields": ["callsign", "band", "mode"],
    "keep": "first"
  },
  "group_by": [
    {"field": "datetime", "interval": "minute", "size": 10, "as": "period"}
  ],
  "metrics": [
    {"function": "count", "as": "accepted_qsos"},
    {
      "function": "count",
      "filters": {
        "conditions": [{"field": "continent", "op": "neq", "value": "EU"}]
      },
      "as": "outside_europe_qsos"
    },
    {
      "function": "distinct_count",
      "fields": ["dxcc", "band"],
      "as": "entity_band_units"
    }
  ]
}
```

The LLM applies the external point values and formula to these counts. QLog MCP neither
labels the retained rows as official duplicates nor calculates an official score.
