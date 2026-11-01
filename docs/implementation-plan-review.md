# Implementation plan: repository evaluation and revised delivery plan

Reviewed 2026-11-02 against `8512b42` (`Merge pull request #6`).
Source proposal: [Implementation_plan.md](../Implementation_plan.md).
This is a review and implementation plan, not an implementation of Milestone 1.
Breaking API and schema changes are allowed; historical user data must still have
an explicit migration path. Commit subjects below are proposed, not executed.

## Assessment

**The direction is sound, but the original draft is not ready to implement as
written.** Independently versioned preferences, job content, reference data,
configuration, and algorithm identity fit the project. The largest risks are a
parallel storage architecture, incomplete ranking inputs, and integrity promises
that the sample schema does not enforce.

Keep the six dimensions, separate fit from evidence confidence, keep unknown values
distinct from zero, and defer survey interaction and scoring. Reuse the existing
domain/services/infrastructure split and extend the existing database. Do not
copy the illustrative Python and SQL blocks directly into production.

## Current state

| Area | Repository evidence | Consequence for the proposal |
| --- | --- | --- |
| Toolchain | [pyproject.toml](../pyproject.toml) declares Python >=3.10, Pydantic >=2, Hatchling, and a `dev` dependency group; `uv.lock` exists | No package-manager migration or new Pydantic dependency is needed. Use `uv` throughout. |
| Architecture | [domain boundary tests](../tests/test_domain.py), `domain/`, `services/`, `infra/` | Put pure contracts in `domain/`, orchestration in `services/`, and SQL in `infra/store/`. |
| Database | [Store and MIGRATIONS](../src/applicant/infra/store/sqlite.py), especially lines 55–116 and 134–183 | SQLite already has migrations 1 and 2, tracked by `PRAGMA user_version`. The next migration is 3, not a new v001 system. |
| Storage APIs | [repositories.py](../src/applicant/infra/store/repositories.py), [storage.py](../src/applicant/storage.py) | `applicant.storage` is already a module for JSON/CSV storage. Creating a same-named package conflicts with its imports and responsibilities. |
| File interoperability | `Store._sync_listing`, `Store.save_jobs`, `StoreListing`, and [store tests](../tests/test_store.py) | Jobs are mutable current state; externally edited/deleted exports are reconciled back into SQLite. Historical versions must survive those operations. |
| Job model | [domain/job.py](../src/applicant/domain/job.py), lines 96–166 | Fields include `source`, optional `id`, salary text, `remote`, and experience bounds. There is no description, work-mode enum, or technical-requirements field. |
| Identity | [domain/dedupe.py](../src/applicant/domain/dedupe.py) | Board identity and cross-board application deduplication already differ. Neither is the proposed content hash. |
| Compensation | [domain/salary.py](../src/applicant/domain/salary.py) | Parsing and annualization factors already exist, but do not preserve the proposed full provenance/gross-net contract. |
| FX and PPP | [domain/rates.py](../src/applicant/domain/rates.py), [money.py](../src/applicant/money.py) | Pure conversion and a per-batch `RateSnapshot` already exist; live/cache retrieval already exists. Snapshots lack durable IDs and complete provenance. |
| CLI and paths | [cli/__init__.py](../src/applicant/cli/__init__.py), [settings.py](../src/applicant/settings.py) | Reuse argparse, settings precedence, error handling, and `applicant.cli:main`; no new `main.py` entry point. |
| Verification | [CI](../.github/workflows/ci.yml), [pyproject.toml](../pyproject.toml) | Tests, type checks, coverage, and saved-page browser checks already exist. Extend them rather than creating a separate pipeline. |

Candidate profiles, durable FX snapshots, scoring configurations, immutable job
versions, and ranking repositories are new work. Existing ratings and financial
history are not yet immutable inputs to a ranking run.

## Findings and required amendments

### 1. P1 — Replace the duplicate persistence architecture

Proposal sections 3, 9, 10, and 14 prescribe a new `storage/` package and migration
ledger despite section 14.1 asking to reuse existing abstractions. Implementing
those literally creates competing owners of `applicant.db` and collides with
`storage.py`. Existing `jobs` rows have `(listing, key)` identity, not `job_id`.

Keep `infra/store/sqlite.py` as the connection/migration owner. Append migration
3 to `MIGRATIONS`; retain `PRAGMA user_version` as the only version authority.
Use sibling repository modules under `infra/store/`. Extract shared connection
helpers only if required by actual callers, preserving a single initialization
path. Do not create `schema_migrations` or renumber shipped migrations.

### 2. P1 — Make job/version/run relationships enforceable

Proposal section 9 independently stores `job_rankings.job_id` and a foreign key
to `job_version_id`. It permits a row labeled job A to reference job B's version.
Its lookup index also does not prevent duplicate versions under concurrent writes.

Use explicit non-null identifiers throughout. Add a unique parent key on
`job_versions(job_id, job_version_id)` and a composite foreign key from results.
Deduplicate exact snapshots with `UNIQUE(job_id, content_hash)` and atomic
insert-or-read behavior. A shared `ranking_hash` must not collapse distinct
content snapshots: it is a freshness hint, not version identity.

Separate immutable profile revisions from the mutable active selection. Use
`profile_heads(profile_id PRIMARY KEY, active_revision)` with a composite foreign
key to the revision table, updated in the same transaction as a new revision.
This replaces `is_active`; it avoids mutating historical rows to select a revision.

### 3. P1 — Define immutable writes and completed-run atomicity

Proposal sections 9 and 11 promise immutable snapshots/results, but the schema
permits updates; there is no run lifecycle or completion boundary. Separate
repository commits can leave partial runs that look complete.

Repositories accept the same caller-owned connection and never independently
commit a composed operation. Validate all inputs before entering the write
transaction. The completion service inserts the run, ordered input membership,
and supplied results as one transaction. Milestone 1 does not expose persisted
draft runs. Failures leave no run or result rows; preexisting immutable inputs
remain reusable. Empty completed runs are allowed and explicitly carry zero inputs.

Add database triggers rejecting updates/deletes to historical tables, including
run membership and results. Store completion in a separate append-only
`run_completions` table, so finalizing never updates an immutable run row.
Reject additions to a completed run through that marker and insert guards; a later transaction cannot
extend an already completed run. Only the service can construct an incomplete run,
and it must mark it complete before committing. Historical reads expose only
completed runs. Test those rules with direct SQL
as well as repositories. This protects against accidental application writes,
not a database owner deliberately removing constraints.

### 4. P1 — Separate job facts from run-dependent normalization

Proposal sections 6, 11.4, and 12 put normalized compensation into an immutable
job version without defining the currency basis or normalization dependencies.
A conversion can change when FX, hours/days assumptions, salary strategy, or the
normalizer changes even if the posting does not.

Job versions hold original salary text, extracted bounds/currency/period/basis,
source payload, and extractor identity. Run-specific normalization belongs in the
result/input bundle and references that run's FX snapshot and scoring config.
Record normalizer version, annual hours and days, representative strategy,
rounding policy, and both source/target currency and gross/net basis. A job hash
must not change merely because a newer exchange rate was used.

Prefer `Decimal` for new money/rate contracts and canonical decimal strings in
JSON; existing float-based search filtering can keep its API through an explicit
adapter. Version the decimal conversion/rounding policy. Do not silently convert
old float cache entries into claims of original decimal precision.

### 5. P1 — Preserve the actual evidence consumed by future scoring

The sample fingerprint in section 12 omits `remote`, `experience_text`,
`experience_min`, and `experience_max`, while referencing `description` and
`work_mode`, which are not current `Job` fields. The enrichment service currently
retains the extracted experience phrase, not the full description.

Build a versioned explicit projection of the real `Job` schema. Include title,
company, location, salary, remote status, employment type, experience evidence,
and every newly introduced field that eligibility/scoring will consume. Keep
source data separate from transient filter flags. Preserve optional description
and extraction metadata when actually available; legacy values stay unknown.
Milestone 1 need not add scraping requests to populate missing evidence.

The five input categories are sufficient only if all auxiliary evidence is inside
them. Before culture/stability/technical scoring exists, embed the exact company
rating, financial, and extraction evidence consumed in the immutable input bundle,
including provenance and observation time. Do not read mutable current ratings
while replaying a run. Until such evidence is captured, these dimensions are
unknown. Persist all selected job versions, including excluded/review-required
ones, with stable input order; preserving only scored jobs loses the selection set.
Result contracts must represent `eligible`, `review_required`, and `excluded`
without inventing zero scores: permit absent scores/confidence when no calculation
was supplied, with explicit status/reason fields and bounds on any supplied values.
Persisting a supplied eligibility state does not implement the eligibility engine.

An algorithm version string identifies code but cannot by itself reproduce it.
Record a build/commit identity and lockfile digest alongside the algorithm version;
retain the corresponding source/release artifact. Milestone 1 proves input
recovery, not replay of an engine that does not exist yet.

### 6. P1 — Historical FX lookup must never substitute current rates

Section 8.2's hierarchy is ambiguous between replay and a new run. Replacing a
missing historical snapshot with the latest cached/static one changes the inputs.
`captured_at` alone also cannot distinguish newly fetched rates from old cached
rates, and existing search supports PPP in addition to market conversion.

Historical loads resolve exact IDs and fail explicitly on missing/corrupt inputs.
Only a new run may select an eligible cache snapshot. Persist provider identity,
rate effective date, retrieved/captured time, provenance (`live`, `cache`, `static`,
or `identity`), and any fallback dataset version. Require an FX snapshot for every
run, including an identity snapshot when no conversion is needed.

Milestone 1 accepts supplied/offline snapshots; it adds no provider. Do not invent
a static FX seed table: none exists today. Keep PPP out of the new ranking contract
until PPP factors, years, sources, and fallback decisions can also be snapshotted.
Existing search PPP behavior remains available. A valid currency code and an
available conversion rate are distinct checks.

### 7. P2 — Complete the contracts before treating examples as specifications

The examples are incomplete: `ScoringConfiguration` uses `Literal` without
importing it; its timestamp is not checked for awareness. `NormalizedCompensation`
does not forbid extra fields, validate original bound ordering, or require units
and provenance for normalized values. The profile has no salary gross/net basis
despite the subsequent comparison requirement. Currency-registry validation is
required in prose but has no selected source or version policy.

Define the following together with the contracts:

- Reject blank identities, unsupported schema versions, unknown fields, naive
  timestamps, NaN/infinity, negative money/rates, and inconsistent ranges. Normalize
  aware timestamps to UTC. Require `utcoffset()` to be non-null too.
- Candidate thresholds are annual base pay; add an explicit gross/net expectation.
  Define empty target lists as unrestricted and document this choice.
- A normalization status distinguishes available, unavailable, and review-needed
  values. Unknown source units/basis cannot produce a falsely comparable result.
  Validate original and annual ranges and positive work-hour/day assumptions.
  Representation consistency is in scope; executing normalization remains deferred.
- Compute configuration hashes internally. Reject caller-supplied mismatches.
  Hash all behavior-affecting fields, including schema/policy versions; exclude
  record IDs, hashes themselves, and creation timestamps.
- Version canonical serialization: UTF-8, sorted object keys, compact JSON,
  no non-finite values, canonical decimals, UTC timestamp representation, explicit
  null/default rules, and field-specific set/list ordering. Do not blindly lowercase
  descriptions or technology names. Golden hash fixtures lock down this contract.
- Use frozen models plus defensive copies and revalidation at repository ingress;
  frozen models alone do not freeze nested lists/dictionaries. Persist canonical
  serialized bytes independent of caller-owned mutable objects.
- Select a versioned currency dataset during the contracts commit; use `uv add`
  for a maintained dataset package if needed, or vendor an attributed snapshot.
  Do not use `CURRENCY_COUNTRY` as the full ISO registry. Historical validation
  uses the recorded registry/policy version, so later registry updates cannot make
  historical data unreadable. Unsupported archived schema versions fail explicitly.

### 8. P2 — Plan upgrade and concurrency behavior for the existing Store

`Store._migrate()` currently reads `user_version` before starting its transaction
and does not reject a future version. Two initializers can both decide to apply
the same DDL. The plan does not address this existing integration issue.

Configure and verify foreign keys before transactions, set a bounded busy timeout,
and initialize/verify WAL for supported on-disk databases. Give unsupported journal
modes a clear error instead of silently claiming WAL; in-memory test databases can
use their documented journal mode. Close connections when initialization fails.
Use `BEGIN IMMEDIATE`, then read/recheck the schema version under the write lock.
Reject versions newer than this application supports. Keep migrations transactional
and do not use `executescript` in a way that escapes the migration transaction.

Profile updates similarly lock before reading the head and require the caller's
expected revision. A stale update raises a conflict without changing either table.
Each concurrent writer gets its own connection. Do not introduce network access
or interactive prompts inside transactions.

### 9. P2 — Specify integration scope and backend behavior

Milestone 1 is a library persistence capability. Add a service that explicitly
captures a supplied set of current jobs and saves supplied ranking results; do not
attach history capture to every search or retrofit historical scrapes that are no
longer available. A future ranking command loads jobs through the existing listing
repository before capturing them. Subsequent listing edits/removals must not alter
captured versions.

Ranking persistence uses SQLite only. Later survey/rank commands must reject
`--store files` with a usage error explaining the requirement; discovery/application
file mode remains supported. Preserve existing settings and path resolution,
including `Store.beside` behavior for custom listing paths. The new service receives
an explicit Store and does not guess which database owns a supplied listing.
Imports and empty read-only commands must continue to avoid creating databases.

## Revised layout

```text
src/applicant/
  domain/
    job.py                     existing Job; optional evidence fields as needed
    salary.py                  existing search parser
    rates.py                   existing pure conversion/snapshot interface
    preferences.py             new candidate contracts
    compensation.py            new compensation/evidence contracts
    ranking.py                 config, version, run, result contracts
    fx.py                      durable FX contract
    canonical.py               versioned serialization and content hashes
  infra/store/
    sqlite.py                  existing connection owner; migrations 1, 2, then 3+
    repositories.py            existing listing/application backends
    profiles.py                new profile/head persistence
    snapshots.py               new FX/config persistence
    job_versions.py            new job version persistence
    ranking_runs.py            new run/membership/result persistence
  services/
    preferences.py             revision orchestration
    ranking_history.py         atomic capture/completion and exact historical load
  storage.py                   existing file storage module
  cli/                         existing argparse entry point; no new M1 commands
tests/
  test_preferences.py
  test_ranking_contracts.py
  test_ranking_store.py
  test_ranking_history.py
  test_store.py                extend existing upgrade/regression tests
```

These are intended ownership boundaries, not an instruction to create empty files.
Extend the architecture tests to cover all new pure modules and keep services
free of direct SQL. Do not create placeholder survey/scoring/eligibility modules.

## Semantic commit sequence

Implementation started on `codex/ranking-foundation`:

- `a3e396a` records the reviewed plan.
- `fd0d4f0` consolidates dependencies and CI tools under the uv lockfile.
- `ddee3b5` enables SQLite foreign keys/WAL, serializes migration decisions,
  rejects future schemas, and closes failed connections, with regression tests.
- `eb67d06` releases CLI-owned log handlers on success, failure, and interruption,
  preserving caller-owned handlers and fixing logging test cleanup order.

SQLite initialization and logging lifecycle changes were pulled forward because
they are independently testable prerequisites and require no new ranking schema.
That initial slice left domain contracts and portability work open.

Validation of this initial slice: focused workflow, migration, and logging tests
pass; repository-wide Ruff checks and formatting checks pass; `uv lock --check`
passes; `uv build` produces the source distribution and wheel. Type checking the
changed Python files passes. Full-project Pyright still reports three existing
Windows errors for POSIX-only `os.geteuid` and `time.tzset` in unrelated tests.
The full regression run reports **970 passed, 12 failed, 13 skipped, 671 subtests
passed** in 53.83 seconds, improving on the reviewed baseline's 23 failures.
Remaining failures are the previously observed Windows permission assertions,
path comparisons, and unescaped saved-search TOML path. Browser checks still skip
because Chromium is not installed locally.

The subsequent feature slice implements the Milestone 1 persistence workflow:

- `cd85e71`: versioned domain contracts, decimal canonicalization, ISO currency
  validation, and hashes using the actual Job fields.
- `8c830c9`: canonical FX effective dates and an exact currency-dataset dependency
  pin, so schema-v1 validation cannot silently switch registries on dependency refresh.
- `0d0cb7d`: migration 3, immutable history tables and completion guards, composite
  job/version foreign keys, caller-owned transactions, and profile revision services.
- `02c6894`: FX/configuration storage, content-addressed job-version reuse, atomic
  run completion, and exact typed input recovery. Snapshots, job versions, and run
  services landed together because the acceptance journeys exercise their shared
  transaction and recovery boundaries.

Acceptance journeys cover concurrent profile editors and configuration writers,
reopened databases, edited/deleted listing exports, all newer current input records,
unknown/unscored jobs, ordered membership, duplicate records, mismatched job/version
references, failed batch rollback, and direct-SQL update/delete/REPLACE guards.

Feature validation: **995 passed, 12 failed, 13 skipped, 777 subtests passed** under
coverage in 56.07 seconds. All failures match the existing Windows baseline; measured
coverage is **98%**. Repository-wide lint and format checks pass, as do type checks
for every new/changed feature module and test, lock validation, and `uv build`.
The built wheel was installed into an isolated uv environment and passed an offline
profile/FX/configuration/run history round-trip, including packaged currency data.

Milestone 1 functionality is implemented at the library/service layer. Its final
validation gate still requires a hosted Linux matrix and saved-page browser run.
The local Windows test and type-check issues described above are now resolved,
as recorded below.
Scoring/eligibility algorithms, normalization calculations, survey interaction, and
ranking CLI remain outside Milestone 1. Latest-cache selection and static fallback
policies are deferred to the normalization engine; this layer requires explicit
snapshots and never substitutes them when recovering history.

Windows portability follow-up:

- `a68b4f7` makes optional POSIX test APIs type-safe and restores the prior timezone
  after the timezone-sensitive test, including assertion failures.
- `16734a6` resolves all 12 Windows failures. Private JSON, credentials, logs,
  databases, and WAL sidecars receive protected Windows owner ACLs; atomic JSON
  replacement preserves existing access policies. Tests inspect real native ACLs
  on Windows and exact mode bits on POSIX. Path fixtures use native absolute paths,
  source inspections use POSIX relative paths, and generated TOML escapes paths.
- The unit CI matrix now retains Linux Python 3.10–3.13 and adds Windows Python
  3.12 with a full Pyright check, an explicit Bash shell, and OS-qualified report
  names. The existing required status gate covers the entire matrix.

Latest local validation: **1,010 passed, 13 skipped, 777 subtests passed** under
coverage in 69.70 seconds, with **98% coverage**. Full-project Pyright passes for
both Windows and Linux targets. Repository lint, formatting, lock validation, and
package build pass. The skips are nine unavailable-Chromium browser tests and
four POSIX-only tests. Hosted GitHub Actions execution remains unverified locally;
these commits have not been pushed.

Each commit includes its own focused tests and necessary documentation. Use
Conventional Commit subjects; use `!` and a `BREAKING CHANGE:` footer only when
that commit actually breaks a public API, persisted format, or supported workflow.

| Order | Proposed subject | Concrete scope and verification |
| --- | --- | --- |
| 1 | `docs(plan): reconcile ranking milestone with repository architecture` | Record this evaluation, scope decisions, dependency boundaries, and acceptance criteria; link it from the source proposal. |
| 2 | `chore(deps)!: make uv the single dependency workflow` | Remove the manually duplicated `requirements.txt`, update pip-based setup instructions, keep `pyproject.toml` and `uv.lock` authoritative, and use locked CI environments. Pin lint/type tool versions in the dev group consistently with CI; add coverage there if replacing ephemeral coverage installs. Update workflow tests with the workflow changes. Breaking footer explains `uv sync` replacing the requirements-file install path. |
| 2a | `test: make path and permission assertions platform aware` | Resolve the observed Windows baseline assumptions: compare Git paths in POSIX form, use platform-native absolute-path fixtures, escape Windows paths in generated TOML, and distinguish POSIX mode-bit checks from Windows access-control verification. Add a Windows unit CI leg; do not merely suppress permission-related behavior checks. |
| 2b | `fix(logging): close owned file handlers before cleanup` | Resolve the reproduced open-log cleanup failures with explicit logger ownership/lifecycle and test teardown. Test repeated CLI/library use and temporary-directory cleanup on Windows. Database cleanup after failed initialization belongs to commit 4. |
| 3 | `feat(domain): add versioned ranking input contracts` | Add candidate, compensation, FX, configuration, job-version, run-membership, result, and evidence contracts. Select/version the currency registry. Define Decimal handling, schema versions, algorithm/build identity, canonical hashes, and the real Job projection. Test invalid inputs, nested mutation isolation, missing evidence, and hash fixtures. Add dependencies only through `uv add`, committing the lockfile with the manifest. |
| 4 | `fix(store): serialize migrations and configure sqlite integrity` | Reuse Store, enable/verify foreign keys and WAL, set timeout, recheck migration versions under lock, reject future schemas, and close failed connections. Test fresh/repeated/concurrent initialization and rollback without altering migrations 1–2. |
| 5 | `feat(store): add immutable ranking history schema` | Append migration 3 for profile revisions/heads, FX/configs, job versions, runs, ordered run inputs, and results. Add non-null IDs, composite FKs, uniqueness, JSON/score checks, completion guards, and immutability triggers. Test upgrade from v1/v2 with populated existing tables, direct-SQL violations, and migration rollback. |
| 6 | `feat(preferences): persist profiles with revision conflicts` | Implement create/load/history/update over caller-owned transactions. Compare expected head under `BEGIN IMMEDIATE`; move the head atomically. Test two independent connections, stale updates, rollback, and unchanged historical payloads. |
| 7 | `feat(store): persist immutable fx and scoring snapshots` | Implement exact-ID reads and insert-or-read config deduplication, provenance checks, and canonical round trips. Test simultaneous identical inserts, supplied hash mismatches, unavailable currencies, and historical reads with network disabled. |
| 8 | `feat(store): capture job versions from existing job records` | Assign deterministic board-scoped identity using the existing key policy, preserve source evidence, deduplicate by content hash, and keep ranking hashes separate. Explicitly record an identity-policy version; no-ID postings whose fallback key changes become new identities. Test reused IDs across boards, missing IDs, material changes, harmless formatting, legacy fields, and listing deletion after capture. |
| 9 | `feat(ranking): persist completed runs with exact input recovery` | Compose existing repositories in one transaction, store all selected jobs and supplied results, finalize once, and load a typed bundle of exact inputs. Test mismatched job/version IDs, duplicate membership, partial failures, append/update rejection after completion, stable ordering, empty runs, and recovery after current data changes. No scoring or eligibility implementation. |
| 10 | `test(ranking): verify historical isolation and upgrade journeys` | Add cross-feature acceptance journeys covering reopened databases, edited/deleted exports, newer profile/config/FX/job records, and no network during recovery. Update README/library examples and CHANGELOG with supported scope and backend constraints. Run the existing quality gates. |

Dependencies are sequential unless proven independent. Contract tests land with
commit 3, schema tests with commit 5, and repository tests with their respective
features; commit 10 is not a deferred test phase. A published migration is never
edited retroactively: subsequent changes append migration 4 and onward.

## `uv` workflow and acceptance gates

The repository already uses `uv`; installation and dependency changes should not
use a second environment manager. Before changing dependency metadata:

```console
uv sync --locked
uv run --frozen pytest -q
```

For intentional dependency changes, use `uv add PACKAGE`, `uv add --group dev
PACKAGE`, or `uv remove PACKAGE` and review both the manifest and lockfile. Use
`uv lock --check` to detect drift. Do not upgrade unrelated packages while adding
the ranking contracts. Preserve the Python >=3.10 floor unless a separate breaking
commit deliberately changes it and updates CI and package metadata together.

After the toolchain consolidation commit, the implementation gate is:

```console
uv sync --locked
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pyright
uv run --frozen pytest tests -q --ignore=tests/test_live_pages.py
uv run --frozen coverage run -m pytest tests -q
uv run --frozen coverage report
uv build
```

Coverage commands above assume coverage was added to the locked dev group in
commit 2. Until then the existing documented command uses `uv run --with coverage`.
Keep CI's Python 3.10–3.13 unit matrix, type-check matrix, 90% coverage floor, and
saved-page browser job. Lint/format are currently report-only in CI; this plan does
not silently change that gate policy. Install the built wheel into a clean `uv`
environment and verify imports and any bundled registry data, then run the history
round-trip against that installation.

Milestone 1 is complete only when a fixture run's exact input bundle and supplied
results survive reopening the database and changes to every current input; stale
profile writes and invalid relationships fail atomically; migrations preserve
existing job/application/rating/financial data; and the existing workflows remain
verified. No live FX acquisition, salary algorithm, survey, ranking calculation,
confidence calculation, or ranking CLI is required for that milestone.

## Review verification

Repository inspection covered the proposal, package metadata, lockfile presence,
domain models, search enrichment, FX/cache handling, settings/CLI integration,
SQLite migrations and repositories, architecture checks, and CI/test conventions.
Executed `uv run --frozen pytest -q` using Python 3.12 on Windows:
**955 passed, 23 failed, 13 skipped, 664 subtests passed**, in 77.01 seconds.
The sandbox initially denied access to uv's interpreter cache; the run succeeded
in starting after the approved escalation. No source or dependency changes were
made during this review.

The failures are preexisting in this checkout, not failures of implemented ranking
code. Observed groups are POSIX permission-bit assumptions; Windows versus POSIX
path comparisons in workflow/domain-boundary checks and settings fixtures; open
log files and a failed-initialization SQLite connection preventing temporary-file
cleanup; and a saved-search output fixture returning CLI exit code 2. The latter
constructs a TOML path and needs escaping/path diagnosis in the portability commit.
Do not claim all failures are harmless test issues: connection and handler
lifecycle failures also warrant implementation fixes.

`uv run --frozen pytest --lf -q --tb=short` reproduced 21 failing test cases; the
two workflow subtest failures from the full run were not selected by that rerun.
Nine skips were the saved-page browser tier because its Chromium executable was
not installed; the remaining four were platform/permission conditions. Browser
behavior, Linux CI, type checks, coverage, and wheel installation were not verified
in this documentation review. The commit plan explicitly accounts for the red
Windows baseline before declaring Milestone 1 complete.
