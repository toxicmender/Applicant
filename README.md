# Applicant

Applicant finds jobs, tracks applications, researches companies, and ranks roles
against candidate preferences. It provides a command-line interface and Python
APIs for discovery, applications, company evidence, and reproducible analysis.

- [Installation](#installation)
- [Usage](#usage)
- [Configuration and storage](#configuration-and-storage)
- [Python API](#python-api)
- [Repository layout](#repository-layout)
- [Development](#development)

## Installation

Install [uv](https://docs.astral.sh/uv/) and run these commands from the repository
root. Applicant supports Python 3.10 and later.

```console
uv sync --locked
uv run playwright install chromium
uv run applicant --version
```

Install the optional PDF dependencies with `uv sync --locked --extra pdf`.
HTTP-only and offline workflows can run without installing a browser.

## Usage

See the [complete examples](docs/cli-usage.md#usage).

<a name="try-an-offline-research-workflow"></a>
- [Try an offline research workflow](docs/cli-usage.md#try-an-offline-research-workflow)

### Job discovery and applications

See the [complete examples](docs/cli-usage.md#job-discovery-and-applications).

<a name="public-job-search-search"></a>
- [Public job search (`search`)](docs/cli-usage.md#public-job-search-search)

<a name="signed-in-linkedin-recommendations-jobs"></a>
- [Signed-in LinkedIn recommendations (`jobs`)](docs/cli-usage.md#signed-in-linkedin-recommendations-jobs)

<a name="applications-apply"></a>
- [Applications (`apply`)](docs/cli-usage.md#applications-apply)

<a name="listing-and-application-totals-status"></a>
- [Listing and application totals (`status`)](docs/cli-usage.md#listing-and-application-totals-status)

### Providers and sessions

See the [complete examples](docs/cli-usage.md#providers-and-sessions).

<a name="provider-catalog-and-health-sources"></a>
- [Provider catalog and health (`sources`)](docs/cli-usage.md#provider-catalog-and-health-sources)

<a name="provider-authentication-setup"></a>
- [Provider authentication (`setup`)](docs/cli-usage.md#provider-authentication-setup)

### Company and job research

See the [complete examples](docs/cli-usage.md#company-and-job-research).

<a name="company-reviews-reviews"></a>
- [Company reviews (`reviews`)](docs/cli-usage.md#company-reviews-reviews)

<a name="funding-and-financial-history-financials"></a>
- [Funding and financial history (`financials`)](docs/cli-usage.md#funding-and-financial-history-financials)

<a name="verified-company-evidence-for-ranking-company-evidence"></a>
- [Verified company evidence for ranking (`company-evidence`)](docs/cli-usage.md#verified-company-evidence-for-ranking-company-evidence)

<a name="company-identity-research-imports-and-reports-research"></a>
- [Company identity, research imports, and reports (`research`)](docs/cli-usage.md#company-identity-research-imports-and-reports-research)

<a name="cross-board-feeds-feed"></a>
- [Cross-board feeds (`feed`)](docs/cli-usage.md#cross-board-feeds-feed)

<a name="technology-demand-and-compensation-analytics"></a>
- [Technology demand and compensation (`analytics`)](docs/cli-usage.md#technology-demand-and-compensation-analytics)

<a name="report-export-and-rendering-report"></a>
- [Report export and rendering (`report`)](docs/cli-usage.md#report-export-and-rendering-report)

### Candidate ranking and feedback

See the [complete examples](docs/cli-usage.md#candidate-ranking-and-feedback).

<a name="candidate-preferences-survey"></a>
- [Candidate preferences (`survey`)](docs/cli-usage.md#candidate-preferences-survey)

<a name="ranking-and-replay-rank"></a>
- [Ranking and replay (`rank`)](docs/cli-usage.md#ranking-and-replay-rank)

<a name="candidate-judgments-feedback"></a>
- [Candidate judgments (`feedback`)](docs/cli-usage.md#candidate-judgments-feedback)

<a name="evaluation-evaluate"></a>
- [Evaluation (`evaluate`)](docs/cli-usage.md#evaluation-evaluate)

<a name="weight-suggestions-personalize"></a>
- [Weight suggestions (`personalize`)](docs/cli-usage.md#weight-suggestions-personalize)

### Benchmarks and exchange rates

See the [complete examples](docs/cli-usage.md#benchmarks-and-exchange-rates).

<a name="wage-benchmarks-benchmark"></a>
- [Wage benchmarks (`benchmark`)](docs/cli-usage.md#wage-benchmarks-benchmark)

<a name="comparing-pay-across-currencies-rates"></a>
- [Comparing pay across currencies (`rates`)](docs/cli-usage.md#comparing-pay-across-currencies-rates)

### Database maintenance

See the [complete examples](docs/cli-usage.md#database-maintenance).

<a name="schema-upgrades-db"></a>
- [Schema upgrades (`db`)](docs/cli-usage.md#schema-upgrades-db)

<a name="wal-checkpoints-store"></a>
- [WAL checkpoints (`store`)](docs/cli-usage.md#wal-checkpoints-store)

## Configuration and storage

See the [complete examples](docs/cli-usage.md#configuration-and-storage).

<a name="usage-notes-for-scripting"></a>
- [Usage notes for scripting](docs/cli-usage.md#usage-notes-for-scripting)

## Python API

### Job discovery and application previews

```python
from applicant.filters import JobFilter
from applicant.search import Jobs

with Jobs() as boards:
    jobs = boards.search(
        'machine learning engineer',
        JobFilter(location='India', experience=5),
    )
    boards.apply(jobs, log='preview.csv', dry_run=True)
```

Individual adapters also expose `search` and return the same `Job` model. Use
`emit=callback` on the facade to receive progress events such as `BoardSearched`
and `SourceFailed`. Service functions accept explicit stores and dependencies for
embedding or offline tests.

### Company reviews and financials

```python
from applicant.financials import CrunchbaseClient, FinancialsTracker
from applicant.reviews import AmbitionBoxClient

rating = AmbitionBoxClient().fetch('tcs', max_reviews=10)
print(rating.overall_rating, rating.review_count)

funding = CrunchbaseClient().fetch('zomato')
print(funding.total_funding, funding.valuation)
changes = FinancialsTracker('company_financials.json').record(funding)
```

The reviews clients return `CompanyRating`. Financials clients return
`CompanyFinancials` with structured amounts, funding rounds, and source notes.

### Ranking history

The library can now preserve candidate revisions, job content versions, FX
snapshots, scoring configurations, and completed runs in SQLite. It supports both
supplied results and evaluated engine runs. Survey and ranking creation commands
and historical ranking CLI commands are available.

Use an explicit `Store` and the functions in `applicant.services.preferences` and
`applicant.services.ranking_history`. Save profiles with `expected_revision=0` for
the first revision, then pass the revision you edited when saving the next one.
Stale edits raise `RevisionConflict` without changing history.

```python
from applicant.domain.preferences import CandidateProfile
from applicant.infra.store.sqlite import Store
from applicant.services.preferences import save_profile

with Store('data/applicant.db') as store:
    profile = save_profile(
        store,
        CandidateProfile(profile_id='me', experience_years=5, primary_discipline='software'),
        expected_revision=0,
    )
```

For a run, call `save_fx` and `save_config`, then `capture_jobs` with
`JobVersion.capture(job, parser_version="...")` values. These functions return the
persisted records; use their IDs because equivalent configurations and job content
reuse existing records. Construct a `RankingRun` with the exact profile revision,
FX/config IDs, ordered job-version IDs, algorithm/build identity, lockfile SHA-256,
and any supplied `JobRanking` results. `complete_run` saves the run atomically;
`load_history(store, run_id)` recovers its original inputs and results offline.

Money/rates use decimal strings in canonical JSON. FX rates are quote-currency
units per base-currency unit, with a base rate of one. FX provenance, effective
date, and retrieval/capture times are required; even a run without conversion uses
an explicit identity snapshot. Unknown pay and unscored results remain absent.

SQLite preserves immutable historical records and completed runs. Existing JSON/CSV listing
edits and deletions do not alter captured job versions. Ranking history requires
SQLite; existing discovery/application file storage continues to work.

### Ranking compensation

`applicant.domain.ranking_salary.parse_compensation(text, source=...)` extracts
Decimal salary facts and matched text spans. Supply a `FactSource` with source ID,
observation time, extractor version, evidence quality and supporting excerpt.
The returned `SalaryParse` records extraction policy `ranking-salary-1` and reason
codes for missing/ambiguous information. It accepts explicit currency, pay period,
gross/net basis and base-pay component; none of those is guessed from location.
Bare dollar/yen symbols, conflicting units and ambiguous pay tiers need review.
Pass `structured=(...)` to retain independently supplied `CompensationFacts` tiers
instead of parsing their combined text. Preserve the parse diagnostics with the
captured evidence.

Normalize a selected tier using `applicant.domain.normalization.normalize_compensation`:

```python
from applicant.domain.normalization import normalize_compensation
from applicant.domain.ranking_v2 import ScoringConfigurationV2

# facts is a selected CompensationFacts; fx is an explicit saved FXSnapshot/V2.
config = ScoringConfigurationV2()
pay = normalize_compensation(
    facts,
    target_currency='EUR',
    target_basis='gross',
    fx=fx,
    config=config,
)
```

Use the configuration returned by `save_config` when composing persisted results,
so normalization references the stored ID. Annual/month/week factors are 1/12/52;
hourly/daily assumptions default to 2,080 hours/260 days and are configurable.
Cross-rates use `amount * annual_factor * target_rate / source_rate`, with final
half-even rounding to two decimal places. Gross/net and total/base pay are never
converted into one another. Open bounds remain open: available annual endpoints
can coexist with an unknown representative salary. Inspect `status` and `warnings`
before using the result. This pure function performs no FX acquisition or freshness
selection; it requires explicit run inputs. Eligibility and scoring library APIs
are described below.

To select and persist rates for a **new** batch, use `resolve_fx`:

```python
from datetime import datetime, timezone
from applicant.services.ranking_fx import resolve_fx
from applicant.services.ranking_history import save_config

with Store('data/applicant.db') as store:
    config = save_config(store, ScoringConfigurationV2())
    resolution = resolve_fx(
        store,
        target_currency='EUR',
        source_currencies={'USD'},
        config=config,
        evaluated_at=datetime.now(timezone.utc),
        offline=True,
    )
    fx = resolution.snapshot
```

The explicit batch timestamp records acquisition/capture time. Online mode
(`offline=False`, the default) makes one bounded ECB request before considering
persisted cache entries. Offline mode selects only existing market snapshots or
an explicitly supplied static fallback. The default effective-age limit is seven
calendar days. Use `supplied=snapshot` for an exact input; stale/future supplied
rates fail explicitly. Use `static=snapshot` with `allow_static_fx=True` to permit
a versioned static fallback; no static market dataset is bundled.

Inspect `resolution.diagnostics` and `resolution.missing_currencies`. Missing rates
produce a persisted identity-only snapshot when necessary, allowing same-currency
normalization while other salaries remain unavailable. Selected partial snapshots
are never merged across dates/providers. Cached copies keep original retrieval
and effective dates, response metadata, and a parent snapshot ID. Call this service
outside an existing write transaction; network work finishes before persistence.
Historical reads continue to use `load_history` and exact stored IDs.

### Ranking eligibility

`applicant.domain.eligibility_engine.evaluate_eligibility` evaluates captured
`RankingFacts` bundles, a candidate profile, selected normalized compensation,
the engine configuration, and an explicit evaluation timestamp. It returns an
`EligibilityEvaluation` with `decision`, `route_checks`, and preference notes.
The decision is `eligible`, `review_required`, or `excluded`: any explicit failure
excludes; otherwise any material unknown needs review. All job-wide criteria are
reported even when one already excludes the job. No fit score is calculated here.

Salary floors compare compatible annual base-pay bounds with the same half-even
rounding policy. A range wholly below the floor fails; a guaranteed lower bound
meeting it passes; straddling or insufficient bounds need review. Different pay
tiers require explicit `selected_tier=...`, and normalized pay must match the
selected source facts. Hard constraints remain active with zero scoring weight.
Employment requires supported permanent/contract evidence; `FULL_TIME` alone does
not establish permanence. Inferred/fallback/zero-support facts cannot establish
hard eligibility conditions. Future-dated consumed evidence is invalid input.

Work routes combine required modes and geographic permission. Preferred modes
remain soft; v1 profiles have no mandatory modes or declared work country.
`Remote` does not imply worldwide permission. Country restrictions apply to a
considered remote/hybrid route, and unparsed authorization/timezone restrictions
need review. An acceptable onsite alternative can satisfy the route criterion;
commutability, relocation and work authorization are not inferred.

Use the same stable `route_id` for assertions about one route across separate
evidence bundles, and different IDs for genuine alternatives. Supported assertions
are checked for contradictions before route predicates short-circuit. Route traces
mark irrelevant checks `not_evaluated`, preserving conflicts on skipped alternatives.
Missing/incomplete alternatives never produce an exclusion by themselves. Overall
mode/location eligibility is a joint `work_route` criterion; traces identify the
individual mode and remote-location checks, preventing unrelated marginal passes
from being combined into a false eligible route.

Conflicts include source labels, timestamps, normalized values, excerpts and a
verification prompt. `evaluation.decision` can be stored in `JobRankingV2.decision`;
its conflict details survive offline history recovery. The additional route trace
is returned separately so existing v2 historical payloads remain unchanged. Run
orchestration and replay are described below. Interactive and document-based
surveys, ranking creation and historical ranking commands are available.

### Ranking scores and views

After eligibility, call `applicant.domain.scoring.score_job` with the job/version
IDs, profile, captured `RankingFacts` bundles, decision, normalized compensation,
configuration, evaluation time, and optional selected salary tier. It returns a
`JobRankingV2` suitable for existing run persistence. Excluded jobs retain their
decision and have no fit/confidence calculation. Eligible and review-required jobs
can have known scores, known zero scores, or unknown scores.

Six dimensions cover compensation preference utility, preferred work modes,
required-technology overlap, culture, career growth, and employee-reported job
security. Technical tokens use case folding and the configured exact alias map;
Java and JavaScript remain distinct. Conflicting requirements and unsupported pay
tiers remain unknown. Company dimensions use captured category ratings, not the
overall rating, company funding or valuation. Experience/discipline/seniority
remain context rather than invented title-based requirements in this policy.

The overall score averages known scores using profile weights. Coverage is the
known applicable weight divided by total applicable weight; confidence additionally
weights evidence quality. Unknown dimensions lower coverage/confidence, while
zero-weight and inapplicable dimensions leave both denominators. Zero-weight
dimensions skip evaluation. Fit is not multiplied by confidence. A high fit with
limited coverage remains possible: display all three values together. All-unknown
or all-inapplicable results have no overall score and zero confidence/coverage.

`score_dimensions` in `applicant.domain.dimension_scoring` returns individual
dimension results. `aggregate_dimensions` in `applicant.domain.scoring` exposes
aggregation notes and weighted contributions. Dimension scores and overall scores
round half-even to two decimal places; confidence/coverage round to four. Arithmetic
uses the recorded fixed context rather than caller Decimal settings.

`applicant.services.ranking_evidence.capture_rating` accepts an existing
`CompanyRating`, a caller-verified provider company ID, and observation time. It
returns typed ranking-facts evidence plus a defensive copy of the full raw payload.
It performs no company-name matching or network requests. Preserve both returned
evidence records in the job version. Category selection follows configured provider
priority, latest observation within each provider, then evidence hash. Ratings
older than the configured age window (default exactly 180 days) are unknown;
review-count support saturates at 20 by default. Confidence is an evidence-support
index, not a hiring or solvency probability. Ambiguous company IDs remain unknown.

`applicant.domain.ranking_views.ranked_views(results, config=...)` returns separate
`eligible`, `review_required`, and `excluded` tuples. Scored groups sort by rounded
score descending and job ID ascending; unknown scores come last. Excluded jobs
sort by ID. Confidence never silently breaks ties, and input permutation does not
change ranked order. These views preserve the original result values and do not
replace the input order stored in run history.

### Complete ranking runs and offline replay

`applicant.services.ranking.rank_jobs` connects the library workflow. Supply an
explicit `Store`, profile ID/revision, selected `JobVersion` tuple, engine
configuration, evaluation timestamp, retained build identity and uv.lock SHA-256.
FX arguments mirror `resolve_fx`: offline, supplied/static snapshots and an
optional injected provider. It returns a completed `RankingRunV3` in input order.
Use `ranked_views` separately for sorted views.

```python
from applicant.services.ranking import rank_jobs
from applicant.services.ranking_replay import replay_run

run = rank_jobs(
    store,
    profile_id=profile.profile_id,
    profile_revision=profile.profile_revision,
    jobs=versions,
    config=config,
    evaluated_at=evaluation_time,
    build_identity=release_commit,
    lockfile_digest=release_lock_sha256,
    offline=True,
)
verification = replay_run(
    store,
    run.ranking_run_id,
    build_identity=release_commit,
    lockfile_digest=release_lock_sha256,
)
assert verification.matches
```

The service freezes conservative salary/remote/employment facts when typed facts
are absent. It preserves parser diagnostics; `remote=True` is only an inferred
mode hint, and `FULL_TIME` is not permanent employment. Structured facts take
precedence. Unknown evidence schemas remain captured and unconsumed. Duplicate
job identities and invalid metadata fail before run publication. Network work
precedes write transactions, returned deduplication IDs are used throughout, and
excluded/review/unscored jobs retain their membership. Failed calculations can
leave reusable immutable inputs; they never publish a partial run.

Pass `selected_tiers={job_id: 'Senior'}` to freeze explicit pay-tier choices. These
choices belong to run payload schema 3, keeping job hashes unchanged across tier
choices. This is distinct from SQLite schema 3, whose tables need no migration.
Original v1/v2 history remains readable; legacy supplied-result runs are not
retroactively treated as replayable frozen-input engine runs.

Replay reads exact inputs, performs no acquisition or writes, uses the original
timestamp/configuration/choices, and compares canonical results and explanations.
`ReplayVerification` includes recomputed results and mismatched job-version IDs.
Older supplied-result formats raise `UnsupportedAlgorithm`; different build/lock
identities raise `ReplayEnvironmentMismatch`. Corrupt/missing history raises
`StoreError`, with no fallback. Retain the corresponding source/release artifact:
the caller supplies its actual identity; the library compares these identifiers
and does not discover or attest the executing binary automatically.

For a runnable temporary-database example, use `examples/ranking_offline.py` with
`--build-identity COMMIT` and `--lockfile-digest SHA256` through `uv run --frozen
python`. It prints eligibility, fit, confidence, coverage and replay verification
without network requests. Production callers keep their own persistent Store.

For historical reads, use `Store.open_readonly(database_path)` and wrap related
queries in `with store.read_snapshot():`. This requires an existing database at
the current schema and avoids initialization, migration, permission changes and
listing synchronization. SQLite can still use WAL sidecars. Within the snapshot,
`RankingRunRepository(store.db).completed(profile_id=...)` returns validated
completed runs ordered by calculation time descending, then run ID ascending;
omit the profile ID to include all profiles. Existing history/replay services
accept this read-only Store.

`applicant.infra.store.listing_snapshots.listing_snapshot(path, store=store)`
selects an ordinary listing without modifying its file or stored collection.
A present `{"list": [...]}` file takes precedence; every record must validate,
and duplicate job identities or malformed input raise `ConfigError`. Only a
missing file falls back to that collection's stored rows. With neither file nor
store, it returns an empty tuple. These APIs also support the ranking CLI.

## Repository layout

| Module | Responsibility |
|---|---|
| `applicant.cli` | Command parsing, output, and the installed entry point |
| `applicant.boards` | Job-board adapters, guest search, and LinkedIn application flows |
| `applicant.reviews` | Company rating and review adapters |
| `applicant.financials` | Company funding and financial adapters |
| `applicant.domain` | Models, filters, identity, eligibility, ranking, analytics, and reports |
| `applicant.services` | Search, apply, research, ranking, and provider orchestration |
| `applicant.infra` | Browser and HTTP clients, SQLite repositories, and PDF rendering |
| `applicant.search`, `filters`, `settings`, `storage` | Public facade, filters, configuration, and file interchange |

## Development

```console
uv sync --locked --extra pdf
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pyright
uv run --frozen pytest tests
uv run --frozen coverage run -m pytest tests
uv run --frozen coverage combine
uv run --frozen coverage report
uv run --frozen coverage json -o coverage-m4.json
uv run --frozen python tools/check_milestone_coverage.py coverage-m4.json
uv run --frozen python tools/check_research_coverage.py coverage-m4.json
```

Tests use `unittest.TestCase` under pytest, with HTTP fixtures and Hypothesis
properties. Install Chromium to run the saved-page browser tests. CI covers Linux
on Python 3.10–3.13 and Windows on Python 3.12. Native coverage runs enforce the
global, Milestone 4, and research gates.

Run the offline installed-package journeys with:

```console
uv run --frozen --isolated --no-editable python tools/smoke_cli.py --require-installed
uv run --frozen --isolated --no-editable python tools/smoke_ranking.py --require-installed
uv run --frozen --isolated --no-editable python tools/smoke_milestone4.py --require-installed
uv run --frozen --isolated --no-editable --extra pdf python tools/smoke_research.py --require-installed
```

See the [research coverage matrix](docs/research-coverage.md),
[Milestone 4 validation guide](docs/milestone-4-validation.md),
[mutation testing report](docs/mutation-report.md), and
[maintainer guide](docs/maintainers.md) for the checks and release workflow.
