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
   `qso.query` only when the actual QSO rows are needed as evidence.
4. Use `catalog.query` to enrich known references with directory names and metadata, or
   `catalog.match_qso` to calculate matched, missing, and QSO-only sets without moving
   individual QSOs to the client.
5. State the external rule, date window, confirmation definition, duplicate policy, and
   any limitations in the answer.

The examples below are building blocks, not ready-made award calculators.

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

POTA references may contain multiple comma-delimited items. Compare `pota_ref` for the
contacted station and `my_pota_ref` for the logging station in separate calls:

```json
{
  "catalog": "pota",
  "qso_field": "pota_ref",
  "scope": {"station_scope": "all"},
  "relation": "matched",
  "fields": ["reference", "name", "location"]
}
```

```json
{
  "catalog": "pota",
  "qso_field": "my_pota_ref",
  "scope": {"station_scope": "all"},
  "relation": "matched",
  "fields": ["reference", "name", "location"]
}
```

Each comma-delimited park is matched separately. The two summaries can therefore answer
“contacted parks versus parks from which I logged” without downloading the QSOs. Any
activation threshold still comes from external POTA rules and needs `qso.aggregate`.

## SOTA activity followed by catalog enrichment

First let SQLite count QSOs per recorded summit reference:

```json
{
  "scope": {"station_scope": "all"},
  "filters": {
    "conditions": [{"field": "sota_ref", "op": "is_not_empty"}]
  },
  "group_by": ["sota_ref"],
  "metrics": [{"function": "count", "as": "qso_count"}]
}
```

Then pass the returned references to `catalog.query` for directory facts:

```json
{
  "catalog": "sota",
  "filters": {
    "conditions": [
      {"field": "reference", "op": "in", "value": ["OK/PA-001", "W1/AM-001"]}
    ]
  },
  "fields": ["reference", "name", "points", "valid_from", "valid_to"]
}
```

The LLM evaluates the counts and validity dates under the externally obtained SOTA rules.

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
