# Architecture review and a fresh plan

Written against `master` at `4441a79` (after PRs #4 and #5 merged). It has two parts:
**what the application is today**, measured rather than assumed, and **the
architecture it should grow into**, with the reasoning behind each choice and the
alternatives that were turned down.

The short version:

- The core is strong. `Job`, `JobFilter`, the capability table, salary/PPP
  normalisation and the atomic file layer are careful, well tested and well
  documented. Keep them.
- **`master` is red, and nobody can see it.** A merge dropped the CLI's error
  handler, so one test raises a real `KeyboardInterrupt` that stops pytest after
  124 of 480 tests. CI reports but does not gate, so this reads as green.
- **Half the documented logging security isn't wired in.** Two logging modules
  exist. The CLI calls the one without escaping, secret masking or `0600` files.
- The structure has grown by addition. Three product areas (jobs, reviews,
  financials) each repeat the same client, error, fan-out and storage patterns in
  their own way. `cli.py` and `search.py` hold the orchestration, and library
  code prints to stdout.

The plan: a layered **ports-and-adapters** layout. It has one `Source` abstraction
for all three areas, one error hierarchy, a shared transport layer, SQLite as the
single store (with the CSV and JSON files kept as exports), and a thin CLI. It is
delivered in six phases, each shippable on its own.

---

## 1. How this was evaluated

- Read every module under `src/applicant/` and the two existing plans
  (`docs/board-parity-plan.md`, `docs/ci-plan.md`).
- Ran the project's own checks on a fresh `uv sync`:

| Check | Result |
|---|---|
| `ruff check .` | clean |
| `pyright` (basic, 3.10) | 0 errors |
| `pytest` (as CI runs it) | **1 failed, 123 passed, then the session aborted** (`KeyboardInterrupt`) |
| `pytest --deselect tests/test_error_handling.py::LastResortHandlerTest` | **4 failed, 473 passed** |

- Traced the failures through `git log` and the merge commits.

Size: about 5,900 lines of source in 39 modules, and about 4,300 lines of tests in
16 files (480 tests plus 374 subtests).

---

## 2. What exists today

```
                         cli.py (729 lines: parsing + 7 handlers + orchestration)
          ┌──────────────┬──────────┴─────┬───────────────┬──────────────┐
          ▼              ▼                ▼               ▼              ▼
     search.Jobs    reviews/*        financials/*      money.rates    storage
   (facade: board   AmbitionBox      Crunchbase        FX + PPP       JSON + CSV
    factory, fan-   Glassdoor        Tracxn            (network on
    out, want/                       FinancialsTracker  cache miss)
    enrich, dedupe,
    apply)
          │
   ┌──────┼────────┬──────────┐
   ▼      ▼        ▼          ▼
LinkedIn Indeed  Naukri   GoogleJobs       filters ─► boards.CAPABILITIES
 httpx + httpx + browser   browser                   ─► money (may hit network)
 own     browser                                     ─► places, salary
 browser fallback
```

### 2.1 What is good and should survive any redesign

1. **One normalised `Job` model** (`models.py`). Pydantic validation, blank→None,
   experience ranges derived before validation so they are bounded the same way.
   Every board returns it, so everything downstream stays source-agnostic.
2. **Capabilities as data** (`boards/__init__.py`). This separates *"the board
   filtered it"* from *"the board never publishes it"* from *"the posting was
   silent"*. It is the key idea behind correct filtering across the boards and
   should become a first-class part of every source.
3. **"Nothing is ever guessed."** Unknowns are flagged (`-unknown`,
   `-unpublished`, `ppp-unavailable`), not invented or silently dropped. This is
   a product principle, and the new architecture has to make it easy to keep.
4. **Fail-safe persistence** (`files.py`). Writes go to a temp file, then `fsync`,
   then `os.replace`. Unreadable files are quarantined, not overwritten. CSV cells
   are neutralised against formula injection.
5. **Per-source isolation.** One board or site failing never discards the others'
   results.
6. **Testability.** Clients take an injected `httpx.Client`, `Rates(offline=True)`
   keeps tests off the network, and parsers run on fixtures.
7. **Documentation of *why*.** Docstrings explain decisions, not just behaviour.

### 2.2 Defects found (fix these first, whatever else happens)

| # | Severity | Finding | Evidence |
|---|---|---|---|
| D1 | **High** | **The CLI's last-resort error handler was lost in a merge.** `main()` calls `handler(args)` with no `try`. An unexpected error prints a raw traceback, and Ctrl-C does not exit 130. | Added in `e1cf7be`. Dropped by the conflict resolution in merge `dc35bbb` (7 conflicted files, with `cli.py` among them). `cli.py:729` |
| D2 | **High** | **Because of D1, the test suite aborts.** `test_ctrl_c_exits_130_quietly` makes the mocked handler raise `KeyboardInterrupt`. Nothing catches it, so pytest stops the whole session after 124 of 480 tests. | `tests/test_error_handling.py:92` |
| D3 | **High** | **CI can't show D1 or D2.** The `ci` workflow is deliberately non-gating (`ci.yml` header: "nothing here gates"), so `master` has been red since the merge with every check green. | `.github/workflows/ci.yml:1-8`, `:154` |
| D4 | **High** | **Two logging modules. The CLI wires in the weaker one.** `logs.py` holds everything the README promises (stderr, UTC, control-character escaping, secret masking, a `0600` log file, httpx at `-vv` only). `cli.py` calls `log.configure()`, which logs to **stdout**, does not escape or mask, and creates the file with default permissions. `crunchbase.py:111` and `tracxn.py:145` call `logs.register_secret()`, which does nothing because no `SafeFormatter` is ever installed. **An API key in an httpx error message can reach the log file unmasked.** | `cli.py:15`, `log.py`, `logs.py`. README has two sections ("How much it says", "Logging") that describe different, conflicting `-q`/`-v` semantics |
| D5 | Medium | 4 more failing tests hidden behind D2: board isolation (two), `silence()` keeping the NullHandler, and API keys on the command line never being logged. Their subjects are the same merge and D4. | test IDs in §1 |
| D6 | Medium | `refresh_factors()` writes `ppp_factors.json` **into the package directory** (`Path(__file__).with_name(...)`). That works from a git checkout. Installed as a wheel, it writes into `site-packages` (or fails there). | `money.py:41`, `:248-303` |
| D7 | Medium | Glassdoor writes a logged-in `storage_state.json` (session cookies) into **whatever the cwd is**, with a fixed name, beside the profile directory it already keeps. | `reviews/glassdoor.py:177-181` |
| D8 | Low | `LinkedIn` defines `logger` twice and logs several events twice (`login`, `scrape_jobs`). It launches bundled Chromium directly, bypassing `browser.py`'s Chrome→Edge→bundled fallback that the README says the boards rely on. `state=` is stored and never used. `__del__` calls `close()` during GC (already flagged in `ci-plan.md` §2.6). | `boards/linkedin.py:37,45,81,183-205,462` |
| D9 | Low | `reviews` overwrites `company_reviews.json` every run, while jobs and financials merge and track history. | `cli.py:94` |
| D10 | Low | Easy Apply goes through a staging file. `Jobs._easy_apply` writes jobs to `applied_via_jobs_interface.json` so `LinkedIn.easy_apply(filepath)` can read them back. | `search.py:401-404`, `linkedin.py:377` |

### 2.3 Structural problems (why the defects happen)

**S1: One pattern, implemented three times.** Jobs, reviews and financials each
have:

| Concern | Jobs | Reviews | Financials |
|---|---|---|---|
| Error base | `models.JobsError` | `reviews.errors.ReviewsError` | `financials.errors.FinancialsError` |
| "Blocked" error | `BlockedError` | `ChallengeError` | `ChallengeError` |
| Not found / parse | — | `CompanyNotFound`, `ParseError` | `CompanyNotFound`, `ParseError` (separate classes, same names) |
| Client interface | `Board` Protocol (`search`) | implied (`fetch`) | implied (`fetch`) |
| Fan-out and isolation | `Jobs.search` loop | `cli.run_reviews` loop | `cli.run_financials` loop |
| Source selection | `Jobs._client` if-chain | `cli._review_client` | `cli._financials_clients` |
| Models | Pydantic | **dataclasses** | Pydantic |
| Persistence | `storage` (merge) | `json.dump` in the CLI (overwrite) | `FinancialsTracker` (history) |

Every fix (isolating failures, masking secrets, handling 429s, closing browsers)
has to be made three times, and has been, unevenly.

**S2: Transport is re-implemented per client.** There are four separate retry
loops: `AmbitionBoxClient._get`, `CrunchbaseClient._get`, `TracxnClient._post` and
`money.fetch_factor`. LinkedIn has none. Delays are hard-coded per class. There are
two browser launch paths (`browser.browser()` and `LinkedIn._start`). Nothing
limits request rates across the process.

**S3: Hidden I/O in the domain.** `JobFilter._salary_ok` calls `money.convert`,
which can reach frankfurter.dev or the World Bank on a cache miss. So "is this job
a match?" can block on the network, and the filter is only testable because
callers remember to pass `Rates(offline=True)`. `filters` imports `boards` for
capabilities, so the domain depends on the adapter package.

**S4: Orchestration lives in the CLI.** `cli.py` is 729 lines. `run_apply` repeats
the filter loop that `Jobs.apply(filters=...)` already has. `run_financials` holds
the rate-limit sleep, the source-routing rule (`'tracxn.com' in company`) and the
change printing. None of this can be reached by a library user.

**S5: Library code prints.** There are 22 `print` calls outside `cli.py`, for
example `LinkedIn.login`, `_apply_one` and `Jobs._easy_apply`. The README says
"the library logs, the commands print", but the code doesn't follow it, and
embedding the library puts text on the host's stdout.

**S6: State is scattered across the working directory.** It is spread over nine
places: `job_listing.json`, `applied_jobs.csv`, `company_reviews.json`,
`company_financials.json`, `.money_cache.json`, `applied_via_jobs_interface.json`,
`cookies.json`, `storage_state.json` and three profile directories, plus `logs/`.
Each module picks its own default path. Dedupe re-reads the whole CSV on every
`record()`. Cross-file questions ("which companies I applied to raised money this
year?") need ad-hoc joins.

**S7: The merge process is the weakest link.** D1, D4 and the duplicated LinkedIn
lines all come from branch merges in which two parallel features touched the same
files, and nothing gated. The architecture should reduce how many files a feature
has to touch, and CI must gate what matters.

---

## 3. Target architecture

### 3.1 Shape

```
┌──────────────────────────────────────────────────────────────────────────┐
│ interfaces/     cli/ (argparse, one module per command)  ·  render/      │
│                 → parse args, build Settings, call a service, render     │
├──────────────────────────────────────────────────────────────────────────┤
│ services/       SearchJobs · ApplyToJobs · FetchReviews · TrackFinancials│
│  (use cases)    RefreshRates · Status                                    │
│                 → orchestrate; emit events; no I/O of their own          │
├──────────────────────────────────────────────────────────────────────────┤
│ domain/         Job · CompanyRating · CompanyFinancials · Money · Salary │
│  (pure)         filtering (checks + verdicts) · dedupe/fingerprint       │
│                 places · dates · experience · ports (Protocols)          │
│                 → no httpx, no playwright, no files, no clock/network    │
├───────────────────────────────┬──────────────────────────────────────────┤
│ sources/        (adapters)    │ infra/                                   │
│  jobs/  linkedin indeed       │  http.py      shared client, retry, 429, │
│         naukri googlejobs     │               per-host rate limit        │
│  reviews/ ambitionbox         │  browser.py   one launcher, profiles,    │
│           glassdoor           │               bot-check detection        │
│  financials/ crunchbase       │  store/       SQLite repository +        │
│              tracxn           │               CSV/JSON exporters         │
│  rates/ frankfurter worldbank │  logging.py   the one logging module     │
│  registry.py                  │  settings.py  paths, keys, defaults      │
└───────────────────────────────┴──────────────────────────────────────────┘
      Dependencies point downward and inward only: interfaces → services →
      domain ← sources/infra. The domain imports nothing from the others.
```

### 3.2 Package layout

```
src/applicant/
  __init__.py            version, NullHandler, the public API re-exports
  errors.py              the single error hierarchy (§3.3 D2)
  settings.py            Settings: data dir, paths, keys, delays (§3.3 D7)

  domain/
    job.py               Job (unchanged fields) + parse-once Salary/experience
    company.py           CompanyRating, Review, CompanyFinancials, FundingRound, Money
    capability.py        Capability, Field enum
    flags.py             Flag constants: EXPERIENCE_UNKNOWN, PPP_UNAVAILABLE, ...
    filtering.py         Check protocol + TitleCheck, SalaryCheck, ...; JobFilter
    dedupe.py            fingerprint, reach, one_per_job (from storage/search)
    places.py dates.py salary.py experience.py   (moved as is)
    ports.py             Protocols: JobSource, ReviewSource, FinancialsSource,
                         RateProvider, JobRepository, ApplicationLog, Applier

  sources/
    registry.py          name -> factory + capability; used by CLI and services
    jobs/linkedin.py     guest search + describe (read-only)
    jobs/linkedin_apply.py  login, session, easy_apply (the only writer)
    jobs/indeed.py naukri.py googlejobs.py
    reviews/ambitionbox.py glassdoor.py
    financials/crunchbase.py tracxn.py parsing.py
    rates/frankfurter.py worldbank.py

  infra/
    http.py              HttpClient: headers, timeouts, retry/backoff, 429,
                         per-host min interval, secret registration
    browser.py           BrowserFactory: channel fallback, profiles,
                         storage-state location, looks_blocked
    store/sqlite.py      schema + migrations; repositories
    store/export.py      applied_jobs.csv (Sheets-safe), job_listing.json
    store/files.py       atomic write + quarantine (today's files.py)
    logging.py           merged log.py + logs.py (§3.3 D6)

  services/
    events.py            Event types the services emit (progress, result, warning)
    search.py            SearchJobs: fan-out, want/rounds, enrich
    apply.py             ApplyToJobs: filter, one_per_job, route to Applier, log
    reviews.py           FetchReviews
    financials.py        TrackFinancials (tracker diff/merge logic lives in domain)
    rates.py status.py

  cli/
    __init__.py          main(): parse → configure logging → run → exit code,
                         last-resort handler
    common.py            shared flag groups (filters, logging, output)
    search.py apply.py reviews.py financials.py rates.py status.py jobs.py
    render.py            turns service events/results into terminal output
```

### 3.3 The decisions, and why

#### D1. Layered ports-and-adapters, not a rewrite into a framework

**Choice:** four layers (domain, sources/infra, services, interfaces), with
`typing.Protocol` ports in the domain and dependencies pointing inward.

**Why:**
- **The domain is already the most valuable and most tested code.** Filtering,
  salary normalisation, PPP, places and dedupe hold the project's subtle
  correctness. Making that layer pure (no network, files or clock) turns today's
  "remember to pass `Rates(offline=True)`" (S3) into a guarantee. It also lets
  the domain be type-checked at pyright `strict`, which `ci-plan.md` already
  wanted for "utils only".
- **The sources are the most volatile code.** Scrapers break whenever a site
  changes its markup or bot defences. Isolating them behind a port means a broken
  Glassdoor parser is a one-file fix that can't touch filtering.
- **Services give library users what the CLI has today** (S4). Anything a
  command can do, a Python caller can do with the same call.
- The project already works this way informally: `Board` is a Protocol,
  `Capability` is data, clients take injected `httpx.Client`s. This formalises
  what exists.

**Rejected:**
- *A plugin system via entry points*: there are 10 sources, all in-tree. A static
  registry is simpler, and pyright can see through it.
- *A DI container*: constructor injection plus a small registry is enough at this
  size. A container would hide the wiring that the explicit factories make easy to
  read.
- *A big-bang rewrite*: 480 tests pin current behaviour, and they are the safety
  net. The migration (§4) moves code, not behaviour.

#### D2. One `Source` abstraction and one error hierarchy for all three areas

**Choice:**

```python
# domain/ports.py
class JobSource(Protocol):
    name: str
    capability: Capability

    def search(self, query: JobQuery) -> list[Job]: ...
    def close(self) -> None: ...


class DescribesPostings(Protocol):  # optional, checked with isinstance
    def describe(self, job: Job) -> str | None: ...


class ReviewSource(Protocol):
    name: str

    def fetch(self, company: str, *, max_reviews: int) -> CompanyRating: ...


class FinancialsSource(Protocol):
    name: str

    def accepts(self, company: str) -> bool: ...  # replaces 'tracxn.com' in company
    def fetch(self, company: str, *, max_rounds: int) -> CompanyFinancials: ...
```

```python
# errors.py
ApplicantError
├── SourceError(source: str)
│   ├── Blocked          (bot check, captcha, login wall, 429)   exit 3
│   ├── NotFound         (company/posting does not exist)        exit 4
│   ├── Unparseable      (layout changed; carry a snippet id)    exit 5
│   ├── AuthFailed       (bad key, missing plan)                 exit 6
│   ├── QuotaExhausted   (Tracxn credits; do not retry)          exit 6
│   └── Unreachable      (transport; retried before raising)    exit 7
├── StoreError
└── ConfigError
```

Compatibility aliases are kept for a release: `JobsError = SourceError`,
`BlockedError = Blocked`, and `reviews.ChallengeError = Blocked`. (Removed in
0.2.0, as planned; `Unreachable` got exit 7 in the same release.)

A single `services.fan_out(sources, call)` does the per-source isolation that is
currently written three times. It catches `SourceError` as expected. Any other
exception is logged with its traceback at debug level (ASVS 16.5.2, as now), and
the result carries the per-source outcomes.

**Why:**
- It fixes S1 at the root. Isolation, masking and retry policy get written once,
  and the next source (Wellfound, Instahyre, Levels.fyi…) costs one adapter file
  and a registry line.
- `except SourceError` in services, with distinct exit codes in the CLI, lets
  scripts tell "blocked, try later" apart from "the parser broke, file a bug".
  Today both are exit 1.
- `QuotaExhausted` makes "do not retry" part of the type instead of a code
  comment in `tracxn.py`.
- A `JobQuery` value object (keywords, location, limit, posted_within_days)
  replaces the positional `search()` signature. Some boards currently accept
  arguments and `del` them.

**Rejected:** *one generic `Source[Q, R]` for all three.* Jobs search and company
lookup differ in shape (a list from a query, compared with one record for one
entity). A generic would be abstract in the ways that don't matter. Three small
Protocols that share errors, transport and fan-out capture the duplication without
forcing the shape.

#### D3. Capabilities belong to the adapter, and they are declared, not documented

**Choice:** each adapter declares its `Capability` next to its parser. The
registry exposes `capability(name)` for stored jobs whose adapter isn't loaded.
Capability fields become an enum (`Field.SALARY`, …), not free strings.

**Why:** today `boards/__init__.py` explains capabilities in comments that cite
line numbers (`linkedin.py:124-143`), which drift with every edit. A parser test
can check the declaration: every `Field` the adapter claims to publish appears in
at least one fixture-parsed `Job`. Then the declaration can't silently go wrong.
The enum makes a typo like `'experiance'` a pyright error, not a filter that
quietly never applies.

#### D4. Filtering as a list of checks returning verdicts, with rates injected

**Choice:**

```python
@dataclass(frozen=True)
class Verdict:
    keep: bool
    flag: Flag | None = None
    reason: str | None = None  # for the debug "why dropped" line


class Check(Protocol):
    field: Field  # lets `skip`/capability logic be generic

    def __call__(self, job: Job, ctx: FilterContext) -> Verdict: ...


@dataclass(frozen=True)
class FilterContext:
    today: date
    rates: RateSnapshot  # already-fetched FX/PPP, no I/O
    policy: SilencePolicy  # keep_unknown / keep_unpublished
```

`JobFilter` stays as the public, user-facing configuration. It compiles to a list
of checks. The service fetches a `RateSnapshot` for the currencies it will need
before filtering, then filters purely.

**Why:**
- `JobFilter.matches` is a 60-line method that grows by one `if` block per
  criterion, with the `skip` / flag / drop handling repeated in each block. A
  `Check` per criterion makes adding one (remote-only, employment type, visa)
  self-contained and separately testable.
- Pre-fetching rates removes the hidden network call (S3). It also makes a run's
  conversions consistent: every job in a run is compared at the same rate.
- `Salary` is parsed once, when the `Job` is built (a computed field). Today
  `parse_salary` runs in the filter and again in `ApplicationLog.record`.
- Behaviour doesn't change. `tests/test_filters.py` and
  `tests/test_target_job_filters.py` run unchanged against the new
  implementation, which is the acceptance test for this phase.

#### D5. A shared transport layer: one HTTP client, one browser factory

**Choice:**

- `infra.http.HttpClient` wraps `httpx.Client` and provides: default headers and
  user agent; a timeout; retry with jittered exponential backoff on transport
  errors and 5xx; **429 → `Blocked` with `Retry-After` honoured once**; a
  **per-host minimum interval** (a process-wide token bucket, so four boards, two
  review sites and two financial sources can't burst the same host); and
  `register_secret` for any key it is given.
- `infra.browser.BrowserFactory` is the only place Playwright is launched. It
  covers channel fallback, persistent profiles, where storage state is saved
  (inside the profile directory, owner-only), `looks_blocked`, and a guaranteed
  close through a context manager. LinkedIn's signed-in flow uses it too.
  `__del__` goes away.

**Why:**
- It removes S2's four retry loops and two launch paths, and fixes D7 and D8's
  bypass of the channel fallback. That bypass matters: the README says Naukri and
  Google reject bundled Chromium, and LinkedIn's signed-in flow is the part most
  likely to be fingerprinted.
- **Politeness belongs in the infrastructure, not in each adapter.** These sites
  block aggressively. `--want` doubles pulls and `--from-jobs` may look up hundreds
  of companies. A single rate limiter is what keeps the tool usable, and it must
  not depend on each adapter author remembering a `time.sleep`.
- Tests keep injecting a transport. `httpx.MockTransport` plugs straight into
  `HttpClient`, so the existing fixture-driven tests carry over.

**Rejected:** *switching to async (`httpx.AsyncClient`, Playwright async).* It
would let the boards run concurrently, but: (a) running concurrently against
bot-guarded sites is what gets a client blocked; (b) the slow boards are
browser-bound, and a single user's run is a few minutes at most; (c) it would
touch every test. If wall-clock time becomes a real complaint, the fan-out can run
one worker thread per source (sources are independent, and the per-host limiter
still applies). That is a change to one function, not an async rewrite.

#### D6. One logging module. Library code never prints.

**Choice:** merge `log.py` and `logs.py` into `infra/logging.py`:

| Keep from `logs.py` | Keep from `log.py` |
|---|---|
| stderr console | the per-run default file `logs/run_<ts>.log` and `--no-log-file` |
| `SafeFormatter`: escaping, redaction, UTC `Z` timestamps | delayed file open (no empty files) |
| `0600` log file | `silence()` restoring the embedding program's control |
| httpx/httpcore quiet below `-vv` | idempotent reconfiguration |

The README gets one logging section with one table of `-q`/`-v`/`-vv`.

Services emit **events** (`Progress`, `SourceFailed`, `Applied`,
`FinancialsChanged`, …) through a callback, and never `print`. `cli/render.py`
turns events and results into terminal output. The 22 library `print` calls go
away (S5).

**Why:**
- It fixes D4. That is a security regression: the masking the README documents
  doesn't run.
- Two modules with overlapping names (`log`, `logs`) and different defaults are
  exactly what made the merge in `dc35bbb` go wrong. One module can't be half
  wired.
- Events separate *what happened* from *how it is shown*. The CLI can print
  today, and a JSON-lines output (`--json`), a TUI or a notebook can consume the
  same stream later without touching a service. It also makes "the library logs,
  the commands print" enforceable: a test can assert that `applicant.services`
  and `applicant.sources` contain no `print(`.

#### D7. One `Settings` object and one data directory

**Choice:** a Pydantic `Settings` model, built once in the CLI and passed down,
resolved in this order: flags → environment (`APPLICANT_*`, `CRUNCHBASE_API_KEY`,
`TRACXN_API_KEY`) → an optional `applicant.toml` → defaults. It owns:

- `data_dir` (default `.`, for compatibility; later `$APPLICANT_HOME`, then the
  platform data directory). Every store, cache, profile and log path is derived
  from it.
- API keys (as `SecretStr`, registered for masking when built).
- Per-source politeness: delays and per-host intervals.

The PPP seed stays **read-only** in the package. Refreshed factors go to
`data_dir/ppp_factors.json` and are layered over the seed.

**Why:**
- It fixes D6 (writing into `site-packages`) and D7 (session state in the cwd),
  and ends S6's pattern of each module choosing its own default path.
- `SecretStr` means a key can't be `repr`-ed into a log by accident, which backs
  up the masking.
- `applicant.toml` lets someone save a standing search, such as the eight-title
  AI/ML shortlist that `board-parity-plan.md` describes, rather than retyping
  twelve flags.
- On Python 3.10 (the floor), TOML reading needs the `tomli` backport,
  conditional on `python_version < "3.11"`. That is the only new dependency this
  plan adds.

#### D8. SQLite as the system of record. CSV and JSON become exports.

**Choice:** `infra/store/sqlite.py` holds one database, `applicant.db` in
`data_dir`:

```
jobs(source, source_id, fingerprint, payload JSON, first_seen, last_seen)
      PK(source, source_id_or_hash)   INDEX(fingerprint)
applications(id, source, source_id, fingerprint, status, note, flags, at)
      INDEX(fingerprint)
ratings(source, company_key, fetched_at, payload JSON)
financial_snapshots(source, company_key, taken_at, payload JSON)
rates(kind, key, value, year, fetched_at)
schema_version
```

- `applied_jobs.csv` stays the user-facing artifact, written by an exporter with
  today's exact columns, BOM, `QUOTE_ALL` and formula neutralisation. It is
  regenerated after each `apply`, so a Google Sheets import still works unchanged.
- `job_listing.json` and `company_financials.json` are still exported, for
  compatibility and for people who read them.
- On first run, the existing JSON and CSV files are **imported** (and kept, not
  deleted).
- `JobRepository`, `ApplicationLog` and similar are ports. A `JsonRepository`
  that keeps today's files is also provided and selectable. The tests use an
  in-memory SQLite database.

**Why:**
- **The data is relational and the questions are cross-cutting.** "Companies I
  applied to", "their latest funding snapshot" and "their rating" are three files
  joined by hand today. With SQLite each is a query, and `status` can answer
  them.
- **Correctness under growth.** Today dedupe reads the whole CSV into memory on
  every `record()` (twice: `rows()` and the fingerprint pass), and every
  `save_jobs` rewrites the whole listing. That is fine at 500 rows and slow at
  50,000. Indexed lookups on `(source, id)` and `fingerprint` keep it constant.
- **Atomicity for free.** `files.py` exists to get atomic, crash-safe writes.
  SQLite transactions give that across *several* tables at once. For example, a
  job's application row and its "also-on" merge happen together or not at all,
  which separate JSON and CSV files can't guarantee.
- **History is native.** `FinancialsTracker` reimplements an append-only history
  inside a JSON document. Reviews have none (D9). Snapshot tables make "track
  over time" uniform across all three areas.
- **No new dependency or service.** `sqlite3` is in the standard library, there's
  one file to back up, and it works offline, which suits a single-user CLI.
- The staging file (D10) goes away. The applier takes `list[Job]` directly.

**Rejected:**
- *Keep JSON files only:* this works, but every new cross-cutting feature adds
  another hand-written join and another whole-file rewrite.
- *Postgres, or an ORM such as SQLAlchemy:* a server or a heavy dependency for a
  personal tool is the wrong trade. Raw `sqlite3` with a small hand-written
  migration list is under 200 lines.
- *DuckDB:* good for analytics, but an extra native dependency, and the workload
  is transactional and small.

#### D9. Split read-only scraping from account actions

**Choice:** `sources/jobs/linkedin.py` holds only guest search and `describe`.
The signed-in flows (`login`, `restore_session`, `scrape_jobs`, `easy_apply`) move
to `linkedin_apply.py`, behind an `Applier` port:

```python
class Applier(Protocol):
    source: str

    def apply(self, jobs: list[Job], *, dry_run: bool) -> list[ApplicationResult]: ...
```

**Why:**
- **Different risk classes.** Reading public job cards is low risk. Submitting
  applications on the user's real account is irreversible and reputational. The
  code that can do the second should be small, separate, reviewed on its own, and
  never loaded by `search`.
- Today `LinkedIn` is 463 lines mixing httpx parsing, a Playwright session,
  Selenium cookie conversion and form automation. `Jobs` has to keep the whole
  object alive from `search` to `apply` (`if name != 'linkedin'`) only because
  the same class does both.
- It gives other boards' appliers a place to go later, and the
  `needs_manual_apply` fallback stays the default for any source without an
  `Applier`.
- Safety rules become structural. `dry_run` is a required keyword. The service
  refuses to call a real applier unless the command was run without `--dry-run`
  **and** the user confirmed interactively (or passed `--yes`).

#### D10. A thin CLI, one module per command

**Choice:** keep **argparse**, and split `cli.py` into `cli/<command>.py`. Each
file builds its subparser, turns args into a service call, and renders the result.
Shared flag groups (filters, logging, output) live in `cli/common.py` as argparse
parent parsers. `main()` owns the last-resort handler and the exit codes
(0 ok, 1 nothing found, 2 usage, 3–6 from §D2, 130 interrupt).

The legacy bare-flag form (`applicant -c cookies.json`, the `normalise()`
rewrite) prints a deprecation warning for one release, then is removed.

**Why:**
- Every command file stays around 100 lines, and a new command doesn't touch the
  others, which reduces the merge conflicts behind D1 and D4.
- *argparse over Typer or Click:* it's a new dependency for no capability the
  project lacks. The existing parser and its 31 CLI tests carry over as they are,
  and `normalise()` depends on argparse's behaviour.
- `normalise()` exists so that pre-subcommand invocations keep working. It
  already forced the logging flags onto every subcommand
  (`cli._add_logging` docstring). Retiring it lets global flags come back.

#### D11. Domain models all in Pydantic, with flags as named constants

**Choice:** `CompanyRating` and `Review` move from dataclasses to Pydantic, which
removes the hand-written `to_dict`. Flags (`salary-unknown`, `also-on-indeed`, …)
become constants in `domain/flags.py`. They are still stored and exported as the
same strings, so the CSV vocabulary doesn't change.

**Why:** there is one serialisation path and one validation story. A mistyped flag
becomes an import error instead of a filter that never matches, and the README's
flag tables can be generated from the one list.

#### D12. CI that gates correctness and reports everything else

**Choice:** keep `ci.yml`'s reporting and summary design, but make **tests and
pyright required checks** on `master`. Ruff findings and format drift stay
report-only, as now. In addition:

- run pytest with `-p no:cacheprovider --timeout=120` (pytest-timeout), and
  treat a crashed session (exit code 2/3/4) as a failure distinct from failing
  tests.
- a test asserting that the collected count isn't below a floor, so a suite that
  stops early can't pass.
- an import-linter-style test (plain Python using `ast` or `importlib`, no new
  tool) enforcing D1's layer rules: `domain` imports nothing from `sources`,
  `infra`, `services` or `cli`.
- tests that trigger `KeyboardInterrupt` call `main()` in a subprocess, so a
  regression fails one test rather than aborting the session.

**Why:** D3 is the root cause of D1, D2 and D5 persisting. "Nothing gates" was a
reasonable way to introduce CI without blocking work, but the project now has 480
tests, and a red `master` that looks green is worse than no CI. Gating only the
two signals that indicate *broken behaviour* keeps the low-friction intent:
style never blocks a merge.

---

## 4. Migration plan

Each phase is one PR, leaves `master` green, and changes no user-visible behaviour
unless it says so. Phase 0 is urgent. The rest can be reordered.

| Phase | Scope | Done when |
|---|---|---|
| **0. Restore health** | Put back the last-resort handler (D1). Wire in `logs.setup` and its security properties while keeping `log.py`'s per-run default file (D4). Fix the 4 hidden failures (D5). Dedupe LinkedIn's logger and log lines (D8). Make tests and pyright required in CI (D12). | all 480 tests pass, and CI turns red on a failing test |
| **1. Errors and transport** | `errors.py` with aliases (D2). `infra/http.py` and `infra/browser.py` (D5). Move each client onto them, deleting the four retry loops and LinkedIn's own launcher. Remove `__del__`. | `grep -r "for attempt"` over `sources` is empty, and every existing board/reviews/financials test passes unchanged |
| **2. Domain extraction** | Move the pure modules to `domain/`, turn capabilities into an enum on each adapter (D3), filters into checks with an injected `RateSnapshot` (D4), and flags into constants (D11). Add the layer-rule test. | the domain imports no httpx, playwright or file I/O, and the filter tests pass unchanged |
| **3. Services and events** | `services/*` with `fan_out`. Move orchestration out of `cli.py` and `search.Jobs` (keeping `Jobs` as a thin wrapper for compatibility). Replace library `print`s with events and add `cli/render.py` (D6). Split the CLI into command modules (D10). | there's no `print(` outside `cli/`, and the CLI tests pass |
| **4. Settings and storage** | `Settings` and `data_dir` (D7). The SQLite store with a JSON importer and CSV/JSON exporters (D8). Fix D6, D7, D9 and D10 as they fall out. | a fresh run from an old working directory imports its files, and `applied_jobs.csv` is byte-compatible in its column layout |
| **5. Account actions** | Split LinkedIn into read and apply (D9). Add the `Applier` port, required `dry_run`, and confirmation. Deprecate the bare-flag `jobs` form (D10). | `search` never imports the apply module (a test checks this) |

**Phase 0 status: done.** What was built, and where it differs from the row above:

- `logs.py` was folded into `log.py` now rather than in phase 3. One `configure()`
  gives both halves' properties: stderr, escaping and masking on the console and in
  the file, a `0600` file opened only when there is something to write, the per-run
  default file, httpx only at `-vv`, and `silence()` removing only its own handlers.
  Keeping two modules for a phase would have kept the trap that caused D4.
- `-q`/default/`-v` keep `log.py`'s meaning (warnings / progress / debug), which is
  what the CLI already did. `-vv` adds httpx. The README now has one logging section.
- Commentary moved from stdout to stderr. Three CLI tests that read log lines off
  stdout now read stderr, which is the intended behaviour.
- The Ctrl-C test stays in-process, and there is no test-count floor. CI now fails
  on any non-zero pytest exit, and an aborted session exits 2, so both would only
  duplicate that.
- CI: `types` and `unit` fail when their tool does, and `status` ends with a gate
  step. **Mark `status` as a required check in the branch protection settings**,
  because a workflow file can't do that itself.

**Phase 1 status: done.** No existing test was changed. 47 new tests cover the HTTP
client, the error aliases and the browser session. Where it differs from the row
above, or goes beyond it:

- **There were three browser launch paths, not two.** Glassdoor also had its own
  `sync_playwright` block. All three now go through `infra.browser.BrowserSession`,
  so Glassdoor and LinkedIn's signed-in flow gain the Chrome→Edge→bundled fallback.
- **D7 is fixed here** because it fell out of the move: Glassdoor saves its session
  to `.gd_profile/storage_state.json`, owner-only, not to the working directory.
- **A 429 is always `Blocked`, whether or not it was retried.** LinkedIn and Indeed
  don't retry it: LinkedIn so as not to push through a rate limit, and Indeed
  because it falls back to the browser. The API clients retry it and honour
  `Retry-After` up to 60s.
- **Each client's `delay` is both the backoff base and the gap between requests to
  one host.** The gap is shared across clients in the process. It replaces the
  sleeps between pages in LinkedIn, Indeed and AmbitionBox, and between countries
  in `refresh_factors`. LinkedIn's `describe()` is paced now too; it wasn't before.
- **The World Bank's retry-on-bad-body became a `retry_when` predicate**, so
  `money.py` has no loop of its own. `grep "for attempt" src/` only matches
  `infra/http.py`.
- **`LinkedIn.__del__` is gone.** `LinkedIn` and `Jobs` are context managers, and
  the CLI closes them with `with`.
- **The error aliases are the shared classes, not subclasses.** As a result,
  `except ReviewsError` also catches a job board's failure. Nothing calls both
  inside one `try`.
- **The CLI's 1s sleep between companies in `financials` stays for now.** It paces
  the browser mode too, which doesn't go through `HttpClient`. Phase 3 moves it
  with the rest of the orchestration.

**Phase 2 status: done.** `tests/test_filters.py`, `tests/test_target_job_filters.py`
and every other existing test pass unchanged. A new `tests/test_domain.py` (39 tests)
holds the layer rule and the rest. Where it differs from the row above, or goes
beyond it:

- **The layer rule is a test that reads the source.** It parses every
  `domain/*.py` with `ast` and fails on an import of an I/O library (httpx,
  playwright, os, pathlib, csv, sqlite3, …) or of any layer above, and on any call
  to `open()`. I checked that it catches a real violation. `logging` is allowed.
- **The clock is still a default, not a ban.** `JobFilter.matches(today=None)` and
  `relative_to_iso(now=None)` fall back to the current date when none is given.
  Checks read `today` from the `FilterContext`, so tests and services can pin it.
- **The domain `JobFilter` never fetches.** With `rates=None` it has no figures,
  so a foreign-currency salary is flagged `rate-unavailable` and kept. The
  `applicant.filters.JobFilter` everyone already imports is a thin subclass that
  supplies the live cache and the board capability table, so its behaviour is
  unchanged.
- **Rates are fetched once per batch.** `prepared(filters, jobs)` fetches every rate
  a batch needs in one go (`Rates.snapshot()`), and the facade and `apply` call it
  before filtering. This fixes the per-job FX retry that Phase 1 held back.
- **Capabilities are declared in `boards/__init__.py`, not in each adapter
  module.** The row asked for them on each adapter, but that would make reading a
  stored job's capability import httpx and Playwright. The declarations use the
  `Field` enum, the comments that cited line numbers are gone, and a test parses
  each board's fixtures to check them. `Field` is a `str` enum, so `'posted' in
  capability.filters` still works.
- **The reviews and financials models stay where they are.** D11's move of
  `CompanyRating` to Pydantic isn't in this row. The flag constants are done.
- **Old import paths are re-export shims:** `applicant.models`, `dates`, `salary`
  and `places`. `storage.fingerprint` and `storage._key` now come from
  `domain.dedupe`.

**Phase 3 status: done.** Where it differs from the row above, or goes beyond it:

- **`cli.py` is now the `cli/` package**, with one module per subcommand. Each
  declares its flags, calls a service, and prints the answer. The parser blocks
  moved verbatim, so flags and help text are unchanged. Handlers are still bound
  through `applicant.cli.run_*`, so tests that patch those still work.
- **Services** (`services/`) hold search, apply, reviews, financials, rates and
  status. `fan_out` isolates each source. Events (`BoardSearched`, `SourceFailed`,
  `RatingFetched`, `FinancialsTracked`, `FactorFetched`) carry what the CLI
  renders. `Jobs` is a thin front door over the search and apply services. The
  CLI still goes through `Jobs` for those two, because it's the library's public
  entry point and tests patch `Jobs._client`.
- **No `print` outside `cli/`.** A test walks the package's syntax trees to check
  this, and also that only `applicant/interaction.py` calls `input()`. The
  `--login` pauses and LinkedIn's one-time code now go through an injected
  `Interaction`; the default asks on stderr and stdin. LinkedIn's status lines
  are log records and return values now.
- **Isolation is now uniform, which makes it wider in two places.** Creating a
  board client used to sit outside the per-board `try`, and financials caught
  only `FinancialsError`. Both are inside `fan_out` now, so neither can end the
  whole run. A failing board is logged once instead of twice.
- **Exit codes 3–6 are in `main()`** for a `SourceError` that reaches it:
  blocked 3, not found 4, unparseable 5, key refused or out of credits 6.
- **One existing test changed.** It asserted that `-v` output names
  `applicant.search`; the board loop now logs as `applicant.services.search`.
- **`reviews` writes `company_reviews.json` atomically** through
  `files.write_document`, so a new file is owner-only.
- **The bare-flag deprecation (D10) stays in phase 5** as planned. The staging
  file (D10 of §2.2) stays until phase 4.

**Phase 4 status: done.** Both "done when" checks are tests in `tests/test_store.py`.
An old working directory is imported on first use, and the exported
`applied_jobs.csv` and `job_listing.json` are byte-identical to what the plain
backend writes. I checked that both tests fail when the export or the dedupe is
broken. Where it differs from the row above, or goes beyond it:

- **The database lives beside the files it holds** (`applicant.db` in the data
  dir by default), not at one fixed path. Each listing or log is a collection
  named by its path relative to the database, so two listings stay two listings,
  and moving the directory keeps everything.
- **The sync rule does double duty.** The store records a digest of each export
  it writes. A file that no longer matches (hand-edited, replaced, deleted, or
  never seen) is imported and wins. That rule is the migration, and it also
  guarantees that the file you see is the data the next run uses. An unreadable
  file is left alone by a read and moved aside by a write, as before.
- **One dedupe planner (`storage.plan_rows`) serves both backends.** The CSV
  answers its questions by reading itself; the store answers from indexes. Their
  results are identical, which a test checks.
- **The CLI defaults to `sqlite` and the library to `files`.** Importing applicant
  never creates a database. A read-only command in an empty directory creates
  nothing either; a test run had shown otherwise before that was fixed.
- **`financials` history stays in `company_financials.json`.** `FinancialsTracker`
  already keeps a change log there, and moving it had no payoff this phase.
  Ratings, which used to be overwritten (D9), are the history the database adds.
- **Settings:** flags > env > `applicant.toml` > defaults. API keys in the config
  file are refused rather than read. `tomli` is the only new dependency (for
  Python 3.10 only), and it was already in the lock as a pytest dependency.
- **The fixes that fell out:** D6 (refreshed PPP factors go to the data dir;
  `rates --into` updates the shipped table for maintainers) and D10 (Easy Apply
  takes the jobs directly; the staging file is gone, and its name is kept only for
  old patch targets). D7 was fixed in phase 1.
- **Saved standing searches** (D7 mentions them) are not in `applicant.toml` yet.

**Phase 5 status: done.** The acceptance test is in `tests/test_accounts.py`. It runs a
LinkedIn search through the real CLI in a fresh interpreter, with the network
stubbed, and checks that `applicant.boards.linkedin_apply` was never loaded. A
`--dry-run` apply is checked the same way. I checked that both fail when the
search service imports the account module. Where it differs from the row above,
or goes beyond it:

- **The split keeps every old import working.** `LinkedInGuest` is the read-only
  half. `LinkedIn` is a subclass of it in `linkedin_apply.py`, and
  `from applicant.boards.linkedin import LinkedIn` still works through a lazy
  module attribute, which loads the account module only when asked for. `Jobs`
  searches with a guest client and makes the signed-in one only to submit.
- **The `Applier` port** (`domain/ports.py`) is `apply(jobs, *, dry_run)`, with
  no default for `dry_run`. So is `ApplyToJobs.run`. A dry run never makes a
  client, so it never opens a browser. One applier failing becomes `failed` rows,
  as the Easy Apply path already did.
- **Confirmation lives in the service; how to ask is the caller's.**
  `ApplyToJobs(confirm=...)` is shown exactly the postings about to be sent, and a
  "no" submits nothing and logs nothing for them. The CLI lists them and wants a
  typed `yes`. `--yes` skips the question. With no terminal to ask on, and no
  `--yes`, nothing is submitted.
- **`Jobs.apply()` keeps its old default** (submit) but raises a
  `DeprecationWarning` when `dry_run` is left out. Making it required outright
  would break every existing caller at once. One existing test triggers the
  warning, which is what it is for.
- **The bare-flag form** (`applicant -c cookies.json`) still runs, logs a
  deprecation warning with the `applicant jobs ...` command to use instead, and is
  documented as going away.

## 4a. Where the plan stands

All six phases are done. Measured against §2:

| | Before | After |
|---|---|---|
| Tests run to completion | 124 of 480, then abort | 742, plus 575 subtests, all passing, on 3.10 to 3.13 (87% line and branch coverage) |
| CI | reports only | fails on a test or type error; lint stays report-only |
| Logging modules | two, the weaker one wired in | one: stderr, escaping, masking, `0600` files |
| Retry loops / browser launch paths | four / three | one / one |
| Error hierarchies | three | one; the 0.1.x aliases were removed in 0.2.0 |
| Domain I/O | the filter could reach the network | none, checked by a test that reads the source |
| `print` outside the CLI | 22 | 0, checked by a test |
| Where data lives | nine defaults chosen per module | one data directory; `applicant.db` as the record |
| Code that can act on your account | mixed into the search client | one module, never loaded by a search, confirmed before it sends |

These open items were closed by **0.2.0** (see `CHANGELOG.md`):

- financials history in SQLite
- saved standing searches in `applicant.toml`
- removal of the bare-flag form, `Jobs.apply()`'s default and the rest of the 0.1.x
  compatibility shims

One remains, and only a repository admin can close it: marking `status` as a
required check in branch protection.

Phases 1–3 are refactors behind the existing tests, and the test suite is the
contract. Phase 4 is the only one that changes on-disk formats, which is why it
imports the old files and keeps exporting them.

## 5. What this plan deliberately doesn't change

- **No new board or feature is added.** The plan is structural, and new sources
  get cheaper as a consequence.
- **Every "nothing is guessed" rule is kept:** the flags, the
  `-unknown`/`-unpublished` distinction, and the refusal to invent PPP factors or
  salaries from prose.
- **The public Python API keeps working** during the move: `Jobs`, `JobFilter`,
  the `Indeed()` / `LinkedIn()` constructors and `AmbitionBoxClient().fetch`
  become thin wrappers or re-exports, with deprecation warnings for at least one
  minor version.
- **No live-site tests in CI**, as `ci-plan.md` §2.5 established: GitHub runner IP
  ranges are blocked by every target.
- **argparse, httpx, Playwright and Pydantic stay.** The only added dependency is
  `tomli` on Python 3.10, plus `pytest-timeout` in the dev group.
