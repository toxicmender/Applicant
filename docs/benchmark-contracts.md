# Benchmark imports, acquisition and comparisons

Milestone 4 phase 4 adds schema 6 and the provider-independent foundation.
Phase 5 adds Eurostat acquisition. BLS and MoSPI follow separately. No new
runtime dependencies are required. All commands below use the configured SQLite
database, or an explicit `--database PATH`.

```powershell
uv run applicant db upgrade --database applicant.db
uv run applicant benchmark import --from-file snapshot.json --dry-run
uv run applicant benchmark import --from-file snapshot.json
uv run applicant benchmark show --snapshot RELEASE_ID --json
uv run applicant benchmark show --snapshot RELEASE_ID --snapshot OTHER_RELEASE_ID --export cohorts.json
uv run applicant benchmark compare --run RUN_ID --selections selections.json --export comparison.json
```

Reports use canonical JSON, including Decimal strings. `--json` is accepted for
consistency; these commands always emit JSON. Read-only commands require an
existing current-schema database. Dry-run imports validate without creating a
database or modifying an existing one. Reimporting identical inputs is idempotent;
reusing a snapshot ID for different inputs fails. Exports are atomic/private and
cannot replace the database or WAL/SHM sidecars. Import/show/compare never acquire
FX or refresh providers. Network work requires explicit discover/fetch commands.
With `--no-log-file`, reports do not write a log file either.

## Eurostat acquisition

```powershell
uv run applicant benchmark discover --provider eurostat --export eurostat-dimensions.json
uv run applicant benchmark fetch --query eurostat-query.json --export acquired.json
```

An initial query can be:

```json
{
  "kind": "benchmark_query",
  "schema_version": 1,
  "provider": "eurostat",
  "dataset": "earn_ses22_28",
  "filters": {"geo": ["DE", "FR"], "isco08": ["OC2"]}
}
```

Select geography and ISCO-08 codes explicitly from discovery. `OC2` is the broad
Professionals group, not a software-role or seniority classification. Discovery
returns the source dimension labels, codes, update label, retrieval time and exact
raw response. It creates no database. Fetch validates against fresh discovery,
then sends one selected-data request; repeated filter parameters select multiple
codes. Both raw responses are retained in the immutable snapshot. Existing
databases require schema 6; no additional migration is introduced by the adapter.

The `eurostat-ses-1` policy fixes annual frequency A, gross earnings ERN, enterprises
with 10+ employees GE10, EUR and reference year 2022. Defaults are sex T (total)
and age TOTAL; callers may select other discovered sex/age codes. The query saved
in the snapshot includes all resolved defaults. Unsupported categories and policy
overrides fail explicitly. The API contains bonuses and purchasing-power units
alongside earnings; those alternatives are outside this initial adapter policy.

The Statistics API supplies JSON-stat datasets. The decoder validates dimension
positions and cardinalities, handles array/object category indexes, dense/sparse
values and per-cell/global status strings, and maps cells in row-major order.
Missing values remain unavailable. Confidential observations (legacy c or
restricted confidentiality flags after `|`, including `|C`) are suppressed, with
null amounts; raw flags and bytes remain archived. Other flags such as estimates
or low reliability are preserved. Unexpected dimension sets, truncated vectors,
out-of-range indexes, changed selected categories, provider error/warning bodies
and asynchronous responses fail before persistence. A subsequent explicit fetch
can retry an asynchronous extraction; this adapter does not poll indefinitely.

Accepted responses are limited to 8 MB and cubes to 1,000,000 cells. Selected
fetches are limited to 10,000 cells. Shared HTTP retry/pacing and TLS verification
apply; no browser, new dependency, live fallback or implicit refresh is used.
All provider work finishes before a database writer opens. Failed acquisitions
leave no partial snapshot. HTTP failures retain the existing source exit codes;
invalid provider payloads return exit 5 and invalid selections return exit 2.

Eurostat annual earnings include irregular payments. The adapter retains these
as `gross_annual_earnings_including_irregular_pay`, with source GEO and ISCO-08
codes, NACE Rev.2 B-S excluding O coverage, enterprise size, sex/age and reference
period. Values are published EUR amounts, preserved without an extra FX step.
They appear as context when compared with a posting's base pay. Dataset `updated`
is retained as a release label, not asserted as a publication timestamp; actual
`released_at` remains unknown. Historical reports use stored inputs only.

References: [Eurostat Statistics API](https://ec.europa.eu/eurostat/web/user-guides/data-browser/api-data-access/api-detailed-guidelines/api-statistics),
[JSON-stat specification](https://json-stat.org/format/),
[SES definitions](https://ec.europa.eu/eurostat/cache/metadata/en/earn_ses2022_esms.htm).

## Snapshot document

The complete synthetic example is [snapshot.json](../tests/fixtures/benchmarks/snapshot.json).
Its values are test data, not published wage estimates.

`BenchmarkSnapshot` has `kind=benchmark_snapshot`, `schema_version=1`, an explicit
`snapshot_id`, a `BenchmarkQuery`, `adapter_version`, aware UTC `retrieved_at`,
optional `released_at` and `release_label`, nonempty `responses`, and ordered
`observations` (which may be empty for a successfully completed empty query).
Every contract rejects extra fields. The existing pinned currency registry is
included by the shared contracts. JSON duplicate keys and nonfinite numbers fail
before opening a writer.

Queries identify `provider`, `dataset`, and `filters`, a mapping from nonblank
dimension names to nonempty lists of unique selections. Provider IDs are open
for explicit imports and future adapters. An import is a caller assertion about
the source; it does not independently verify provider authenticity or pagination.
Acquisition adapters must complete and validate all requested pages before
constructing a snapshot. The `BenchmarkProvider` protocol exposes `discover()`
and `fetch(query)`; the first implementation is Eurostat.

Each response preserves `source_url`, exact `body_base64`, and the SHA-256 of the
decoded bytes in `body_sha256`. Its `response_id` is the canonical content hash
of that envelope (including URL), available from `BenchmarkResponse.response_id`.
Observations refer to a response in their own snapshot by this ID. Snapshots
have a digest excluding their caller-supplied ID. Import/storage revalidates all
contracts and digests, including nested objects modified after construction.

Observations contain `observation_id`, `response_id`, `cohort`, original `amount`,
`currency`, `period` (annual/monthly/daily/hourly), `status`, and preserved `flags`
and `notes`. Available values require finite nonnegative amounts. Suppressed and
unavailable values require null amounts. Censored values may retain a published
threshold, but remain context-only.

Cohorts explicitly retain geography and occupation classification/code pairs,
population, industry classification/code, working-time scope, reference start/end,
methodology, wage definition, gross/net/unknown basis, statistic, and additional
source dimensions such as sex, age, or enterprise size. Supported statistics are
mean, median, p10, p25, p75 and p90. Missing classifications must be represented
by a source's explicit aggregate/unknown code; job titles never supply them.
Methodology identifiers must distinguish survey-design breaks. Publication age
and reference periods remain visible; an older release is never silently replaced.

## Normalization and selection

`benchmark-normalization-v1` uses the existing isolated Decimal34 half-even
context and rounds annual amounts to 0.01. Monthly values multiply by 12 and are
labeled annualized equivalents, while their reference periods remain intact.
Daily/hourly conversion requires explicit positive annual days/hours. Nominal FX
uses quote-per-base rates: target/source. Same-currency conversion needs no rate.
Missing rates or assumptions produce null annual values with reasons; suppressed,
unavailable and censored values remain context-only. Unsupported arithmetic
ranges fail clearly. No tax, inflation, PPP or seniority adjustment is inferred.

`benchmark show` groups identical cohorts in identical original currencies and
periods. It retains individual observations and never averages provider estimates
or percentiles. Currency/period groups remain separate until explicit normalization.

Comparison selections have this wrapper:

```json
{
  "kind": "benchmark_comparison",
  "schema_version": 1,
  "selections": [
    {
      "job_id": "FULL_RANKING_JOB_ID",
      "snapshot_id": "RELEASE_ID",
      "observation_id": "OBSERVATION_ID",
      "expected_cohort": {"...": "copy the complete intended cohort here"}
    }
  ]
}
```

The abbreviated cohort above is explanatory; a real selection must include all
required cohort fields from the snapshot. Optional `annual_hours` and `annual_days`
record conversion assumptions. Repeated job/snapshot/observation triples fail;
several distinct cohorts may be selected for a job. Each expected cohort is the
caller's explicit mapping assertion, not a verified classification of the job.

Comparison reads the complete original run and each selected snapshot once in a
read transaction. It uses the run's archived FX, salary currency and basis, selected
salary tier result, and normalized representative base pay. Direct gaps and ratios
require an available observation, matching asserted cohort and salary basis, and
`wage_definition=base_pay`. Other wage definitions are displayed as context with
`wage_definition_mismatch`; missing pay, FX and assumptions receive their own
reasons. A zero benchmark permits a gap but has a null ratio. Excluded jobs may
still have captured pay and can be inspected; this never changes eligibility.

Reports freeze snapshot hashes, exact job versions, selections, normalization
policy and an input digest. Later profile edits, captures or benchmark releases
do not change a report made from the same run and selections. Existing legacy
run formats remain readable through ranking commands; benchmark comparisons
require a modern engine run. Benchmarks never affect ranking or preferences.

## Storage and testing

Migration 6 adds append-only snapshot, response, membership, observation and
completion tables with indexed foreign keys. A completion marker is inserted last
inside the import transaction; completed inputs cannot be extended. Identical raw
envelopes can be shared across snapshots. Recovery checks metadata/child identities,
ordering, raw byte hashes, and complete snapshot hashes. Failed migrations and
imports roll back. Existing historical ranking rows and migration entries are
preserved. CLI writes use the existing private SQLite and atomic file facilities.

Golden fixtures retain cross-currency expectations and separate incompatible wage
definitions. Tests cover arithmetic context isolation and limits, Hypothesis
monthly/annual equivalence, suppression, censoring, assumptions, immutable SQL,
sharing, corruption recovery, upgrades from all five prior schema versions,
migration/import rollback, strict CLI inputs, frozen reports and offline execution.
