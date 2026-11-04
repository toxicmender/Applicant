# Changelog

All notable changes to applicant. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/): while the major version is 0, a breaking
change bumps the minor version.

## Unreleased

### CLI migration

| Previous invocation | 0.3.0 invocation | Change |
| --- | --- | --- |
| `benchmark --query PATH fetch` | `benchmark fetch --query PATH` | Action options follow the verb. |
| `company-evidence --association ID capture` | `company-evidence capture --association ID` | Capture and refresh accept credentials. Offline actions accept their own document or ID options. |


- Record the M4 delivery/coverage audit and fresh public-provider validation
  ledger. Local implementation and gates are delivered; BLS metadata access,
  MoSPI default-TLS acquisition and hosted/live acceptance remain open.

- Gate the full Milestone 4 CLI journey on the existing Linux/Windows installed
  unit legs. Add fixed-clock golden fixtures with forbidden subprocess networking,
  per-module branch-aware coverage enforcement for all 28 M4 runtime modules,
  and regressions for stale suggestions, corrupt acceptance links, legacy runs,
  unknown dimension feedback and invalid feedback cutoffs.

- Add verified company associations and immutable evidence snapshots (schema 7),
  bounded raw capture through all four existing providers, strict retry-safe
  imports, per-provider outcomes and explicit job-version enrichment exports.
  Require structured provider identity during capture, retain unknown source
  dates as context, and keep dated financial facts outside scoring. Enriched
  ranking reports show company provenance; historical inputs remain unchanged.

- Add MoSPI PLFS regular salaried earnings discovery and bounded paginated
  acquisition, with archived code/label mappings, precise monthly INR values,
  Agriculture/Calendar Year distinctions and separate survey-design cohorts.
  Incomplete pages, API errors and hidden cohort changes fail before persistence.
  Keep default TLS verification; live default acquisition currently encounters
  the upstream legacy-renegotiation incompatibility.

- Add BLS OEWS discovery and bounded annual wage acquisition using official
  code tables and the public time-series API. Preserve source footnotes,
  suppression, censoring, annual measure definitions and exact metadata/data
  responses; reject incomplete or warning-bearing responses before persistence.

- Add Eurostat SES 2022 discovery/fetch through shared HTTP, bounded JSON-stat
  decoding with sparse/dense and reordered-dimension support, preserved source
  flags and confidentiality, and immutable raw discovery/selection responses.
  Acquisition completes before any database writer opens.

- Add the benchmark foundation: versioned query/cohort/observation contracts,
  exact raw-response retention, immutable completed snapshots in schema 6,
  Decimal annualization with explicit FX, strict imports/dry runs, and offline
  cohort reports and job comparisons. Provider acquisition follows separately.

- Add bounded, explicit weight suggestions from dimension-importance feedback,
  deterministic archived-score previews, and atomic revision-safe acceptance
  with an immutable audit link (database schema 5).

- Add offline historical ranking comparisons and descriptive feedback evaluation
  with frozen cutoffs, judgment coverage, precision bounds and atomic JSON exports.

- Add append-only candidate feedback with exact ranking membership, revision
  checks, retry-safe event IDs, strict batch imports, read-only historical
  cutoffs and dry runs. Add an explicit database upgrade command for schema 4.

- Add repeatable subprocess and installed-package survey/ranking journeys with
  golden mixed-job fixtures. Gate the installed journey on each Linux/Windows CI
  unit leg and record the completed Milestone 3 delivery/coverage audit.

- Add read-only ranking run lists, exact historical display, detailed job
  explanations and offline replay CLI verification with explicit artifact
  provenance. Show conflicting source assertions and verification prompts;
  preserve legacy formats without recalculation and distinguish absence from
  corruption. Historical JSON output has separate versioned envelopes.

- Add `rank` run creation from strict listing or frozen-version inputs, explicit
  artifact provenance, optional configuration/tier documents and offline FX.
  Validate inputs before opening a writer; report separate eligible/review/excluded
  groups with fit, confidence, coverage and reasons. Display limits preserve every
  result in immutable run history, and JSON keeps explicit null scores.

- Add an interactive candidate questionnaire with explicit hard/soft preference
  prompts, reusable profile validation, salary/weight correction, confirmation,
  safe cancellation and concurrent-edit protection. Prompts use stderr and no
  database connection is held while waiting for answers.

- Add noninteractive `survey` validation, revision-safe imports, dry-run checks,
  historical profile display and atomic editable exports. Preserve Decimal input
  precision and reject malformed/duplicate-key JSON before opening a writer;
  profile edits create new v2 revisions with concurrent-edit protection.

- Add read-only ranking storage access with consistent query snapshots, strict
  listing selection without file synchronization, and validated completed-run
  enumeration with profile filtering and deterministic date/ID ordering.

- Orchestrate complete ranking batches from exact profile/job/configuration inputs,
  normalize and score every selected job, and atomically publish completed runs.
  Replay frozen engine runs offline with original timestamps and tier selections,
  build/lock identity checks, canonical comparison and explicit mismatch reports.

- Add run payload schema 3 for frozen per-job salary-tier choices, preserving
  v1/v2 reads and existing SQLite schema 3. Different run choices reuse the same
  immutable posting version instead of rewriting its content or hash.

- Provide deterministic eligible/review/excluded ranked views with rounded Decimal
  score ties resolved by job ID. Expand coverage of existing ranking contracts and
  corrupted-history recovery, including contradictory exclusion/score states.

- Compute six explainable ranking dimensions with Decimal utility curves,
  independent confidence/coverage, explicit unknown and disabled states, and
  captured category-rating proxies. Preserve raw company-review payloads alongside
  verified-ID facts; never infer culture or stability from an overall rating.

- Evaluate work-mode and remote-location restrictions jointly across alternative
  routes, with conflict classification before safe short-circuiting and explicit
  skipped-check traces. Add three-state job eligibility orchestration, exhaustive
  reference properties, bounded-work assertions, and offline conflict-history tests.

- Evaluate ranking salary floors and permanent/contract eligibility with explicit
  pass/fail/unknown outcomes, conservative open-bound handling, tier selection and
  source/normalization checks. Conflicting supported evidence carries readable
  explanations, source excerpts and verification prompts instead of silently
  choosing one assertion.

- Resolve one persisted FX snapshot for each new ranking batch, with deterministic
  fresh-cache selection, explicit static fallback, identity-only degradation and
  missing-currency diagnostics. Cache recapture preserves original observation and
  retrieval dates and links to its parent; historical loads never refresh rates.

- Acquire ranking FX through Frankfurter v2's ECB provider, validating dated
  EUR-based pairs, preserving exact Decimal tokens and response provenance, and
  disclosing omitted older observations. Provider failures use the shared error
  hierarchy and one bounded HTTP attempt per batch.

- Add pure Decimal base-pay parsing and annual/currency normalization for ranking,
  with explicit missing-evidence diagnostics, open salary bounds, structured pay
  tiers, fixed arithmetic/rounding policies, and recorded FX/configuration references.
  Golden fixtures and property tests cover cross-currency calculations and numeric
  boundaries. Existing search salary parsing retains its behavior.

- Add a gated Windows Python 3.12 unit/type-check CI leg alongside Linux Python
  3.10–3.13, with separate report artifacts for each operating system.
- Enforce private Windows ACLs for new JSON data, browser credentials, log files,
  and SQLite databases/WAL sidecars; preserve existing JSON export permissions.
- Make permission, path, and saved-search TOML tests portable across Windows and
  POSIX, and type-check optional POSIX test APIs safely on Windows.

- Add versioned candidate, compensation, FX, scoring configuration, job-version,
  and ranking-run contracts with offline currency validation and canonical hashes.
- Upgrade SQLite to schema 3 with immutable historical inputs, profile revision
  conflicts, ordered run membership, atomic completion, and exact offline recovery.
- Preserve ranking history when current profiles, rates, configurations, jobs, or
  listing exports change. The library accepts supplied results; ranking calculations
  and survey/ranking commands remain future work.
- Manage dependencies and CI tools through `uv.lock`; install with `uv sync --locked`
  instead of the removed `requirements.txt`.
- Serialize migration decisions and close failed SQLite connections and CLI log
  handlers on completion.

## [0.2.0] - 2026-09-30

A redesign into layers - a pure domain core, shared infrastructure, services, and a
thin CLI - carried out in six phases (see `docs/architecture-plan.md`), and a release
that removes what those phases kept for compatibility.

### Breaking changes

| Before (0.1.x) | Now (0.2.0) |
|---|---|
| `applicant`, or `applicant -c cookies.json` (bare flags), ran the LinkedIn job scrape | a command is required: `applicant jobs -c cookies.json`; no command prints help and exits 2 |
| `-v`, `-q`, `--log-file` only after the command | the logging and data flags go before or after it |
| `applicant jobs -D` / `applicant reviews -D` (`--Display`, which stored False to *show* the browser) | `--show` |
| `applicant jobs --driver PATH` (ignored) | removed |
| `applicant apply` submitted without asking | lists what it will submit and waits for `yes`; `--yes` skips the question; with no terminal and no `--yes`, nothing is submitted |
| `Jobs.apply(jobs)` submitted by default | `Jobs.apply(jobs, dry_run=...)`: required and keyword-only |
| `from applicant.models import Job` | `from applicant.domain.job import Job` |
| `applicant.dates`, `.salary`, `.places` | `applicant.domain.dates`, `.domain.salary`, `.domain.places` |
| `applicant.browser` | `applicant.infra.browser` |
| `JobsError`, `ReviewsError`, `FinancialsError` | `applicant.errors.SourceError` |
| `BlockedError`, `ChallengeError` | `applicant.errors.Blocked` |
| `CompanyNotFound` / `ParseError` / `AuthError` | `NotFound` / `Unparseable` / `AuthFailed`, all in `applicant.errors` |
| `from applicant.boards.linkedin import LinkedIn` | `from applicant.boards.linkedin_apply import LinkedIn`; the read-only search is `LinkedInGuest` |
| `LinkedIn(path=..., state=...)` | removed (unused since Playwright) |
| a Selenium-era `cookies.json` was converted on read | no longer converted: `applicant jobs --overwrite` signs in again |
| `search --limit` etc. parsed to their defaults | unset search flags parse as `None` and take their defaults after any saved search |
| log commentary on stdout | on stderr; stdout carries each command's answer |
| refreshed PPP factors written into the installed package | written to `ppp_factors.json` in the data directory |
| `applicant apply` drove a signed-out browser, so every LinkedIn job was logged "not easy apply, or a multi step form" | it signs in with the session `applicant jobs` saved (`--cookies`, default `cookies.json`); with none, its LinkedIn rows are `failed`, saying to run `applicant jobs` |

**Upgrading a 0.1.x log:** a `needs_manual_apply` row is now final - the job is
not offered again. 0.1.x never signed in to apply, so its LinkedIn rows noted "not
easy apply, or a multi step form" say nothing about the job. Delete those rows from
`applied_jobs.csv` to have 0.2.0 try them (a hand-edited file wins over
`applicant.db`).

### Added

- `applicant.toml`, `--data-dir`, `--store`, `--config` and `APPLICANT_HOME`: one data
  directory, one settings order (flags > environment > file > defaults).
- `applicant.db`: SQLite as the record of jobs, applications, ratings and company
  financials. `job_listing.json`, `applied_jobs.csv` and `company_financials.json` are
  its exports, byte for byte as before. An old directory is imported on first use,
  and a hand-edited file wins.
- Saved searches: `[searches.<name>]` in `applicant.toml`, run with
  `applicant search --saved NAME`, listed with `--list-saved`.
- The history of every rating read, and of every financials change, kept in the
  database.
- `applicant --version`.
- Exit codes that say what ended a run: 3 blocked, 4 not found, 5 unparseable,
  6 key refused or out of credits, 7 network unreachable, 130 interrupted. The
  README has the table.
- `applicant.services`: every command's work, callable from Python, reporting
  through events rather than printing.
- `rates --into` to update the shipped PPP table.

### Changed

- One HTTP client for every source: retries with backoff, `Retry-After`, 429 as
  `Blocked`, and a minimum gap between requests to each host.
- One browser launcher: an installed Chrome, then Edge, then the bundled build, now
  for Glassdoor and LinkedIn's signed-in flow too.
- Exchange rates are fetched once per batch of jobs, not once per job.
- A failing source, including one whose client cannot even be built, costs only
  itself. When every source fails, though, that is the run failing: `search`,
  `reviews` and `financials` (and `Jobs.search` and the services) raise the
  source's error, so the exit code says why - 3 blocked, 4 not found, 7
  unreachable - instead of 1, "nothing matched".

### Fixed

- `master` was red while every check was green: a merge had dropped the CLI's
  last-resort error handler, and pytest stopped after 124 of 480 tests.
- CI now fails on a failing test or type error - and, since the browser sources
  are tested at last, on coverage under 90% or a browser source that no longer
  reads its saved pages in a real Chromium.
- Glassdoor saved a signed-in session into whatever directory it ran from.
- The `[files]` table in `applicant.toml` was read but never used: every command
  hard-coded its file names. Each file and profile flag now defaults to it.
- `financials NAME --from-jobs` fetched a company twice when the listing spelt
  it in another case.
- A dry run (`would_apply`) or a failure (`failed`) in the application log no
  longer stops the real outcome from being logged later, and a job already
  logged as `applied` is not submitted, or offered for confirmation, again.
- A data directory that does not exist yet is created on first write, with
  either store.
- A database or settings error inside a command is reported as what it is,
  not as a crash, and a schema migration lands whole or not at all.
- `applicant jobs` with an expired LinkedIn session says to rerun with
  `--overwrite`, instead of failing on the scrape.
- Easy Apply counts an application only once LinkedIn confirms it; a form it
  refused is left for review instead of being logged as applied.
- `--currency` is read in any case: `inr` is INR.
- A history that cannot be saved stops `financials` once, instead of being
  reported as each source failing in turn.
- `applicant jobs` keeps what it scrapes in the store `--store` names.
- Indeed reports a page that changed as `Unparseable` (exit 5), not `Blocked`
  (exit 3), and a later page that no longer reads keeps the pages before it.
- LinkedIn titles decode every HTML entity (`&#39;`, `&quot;`), so they match
  title filters and the same job on other boards.
- A relative `data_dir` in `applicant.toml` is relative to that file, not to
  wherever the command is run from.
- Glassdoor: the company's rating is never taken from one review's own rating,
  paging counts each review once instead of stopping halfway, and a `--login`
  run asks you to clear the check once, not on every page.
- One Indeed card without a title is skipped instead of failing the search.
- `apply` does not offer a job again once it has an outcome - applied to, or
  found to need a manual application on its own board; a dry run or a failure
  still leaves it to be offered.
- A posting with no location is kept and flagged `location-unverified` by a
  location filter, like any other field a posting leaves out, rather than dropped;
  `--strict` still drops it.
- The AmbitionBox, Crunchbase and Tracxn clients can be closed (and used as context
  managers), and the CLI closes the ones it makes.
- `search` counts (`--limit`, `--want`, `--max-rounds`, `--enrich-limit`,
  `--posted-within`) must be 1 or more, on the command line and in a saved
  search; `--want 0` used to mean "no target".
- `rates --refresh -c XYZ` names a currency it does not know, and exits 2 when
  none of those asked for is known, instead of reporting nothing done.
- A salary range joined by a word - `10 to 15 Lacs`, `$50,000 to $60,000`,
  `45.000 bis 55.000 €` - keeps its top and its unit, and "Negotiable" no longer
  throws away a range that is stated.
- An Easy Apply submission LinkedIn did not confirm is logged as possibly sent -
  "check your LinkedIn applications before applying again" - rather than with the
  note for a job that has no Easy Apply, which read as nothing sent.
  So is one where the page failed after Submit was clicked.
- Ctrl-C part way through `apply` no longer loses the run's log: applications
  already sent, and the rows for the other boards, are recorded before it exits
  130. Jobs it never reached get no row, so the next run offers them.
- A login wall, an expired session or a security check part way through an
  Easy Apply run stops it, and the jobs left are logged `failed` - tried again
  later - rather than each settled as "not easy apply". So is a job that timed
  out or crashed before anything was submitted.
- With `--store sqlite`, an export that could not be read - `job_listing.json`
  or `company_financials.json` - is moved aside as before, but no longer wipes
  the database's copy with it: the file is written again from `applicant.db`.
- `rates --refresh` that got no factor at all exits 1, not 0; `status --json`
  writes its file all at once.
- `--log-file` naming a directory, or a place you cannot write, crashed with a
  traceback on the first log line - the file is only opened then. It is checked
  up front, and the command exits 2 saying so.
- Found by fuzzing the parsers (Hypothesis, `tests/test_fuzz.py`): a posting
  "99999999999999 days ago" crashed the date parser; "10-2 years" became a
  range from 10 to 2; a scraped value already starting `'=` lost its quote when
  the log was read back; and a Unicode line separator (U+2028/U+2029) could
  split one log entry into two.
- Salaries as Indeed's other country sites write them are read correctly:
  `45.000 €` is forty-five thousand, not forty-five; `45 000 €`, `CHF 110'000`,
  `pro Monat`, `par an` and the other local pay periods; `¥`, `CHF`, `AUD`, `CA$`
  and the rest of the currencies a comparison can convert; and a bare `$` on
  ca/au/sg/nz/mx.indeed.com is that country's dollar. A salary is its first
  figure or range, so "plus 401k" no longer rescales it.
- "Today", "Just now", "Just posted", "Yesterday" and "Few hours ago" are dates,
  so `--posted-within` no longer flags (or, with `--strict`, drops) the newest
  postings of all.
- The derived funding-round count is used only when every round was fetched, so
  `-n` no longer understates it or shows up as a change between runs.
- An unreadable rate cache is rebuilt, as its comment always said, instead of
  failing the first salary comparison of a search.
- AmbitionBox keeps the rating and the reviews already read when a later page
  of reviews fails.
- A commit pushed by the formatting workflow gets its own CI run, so the
  `status` check exists on it and cannot block a merge by never arriving.

### Security

- Log files are owner-only, API keys are masked, and scraped text is escaped
  against log forging, in the log file and on the console alike. The logging module
  that did this had not been wired in.
- `applicant.db` and every file applicant creates are owner-only, including a
  new `applied_jobs.csv` on either store.
- The LinkedIn session saved by `applicant jobs` (`cookies.json`) is owner-only,
  as other saved sessions already were, and an older, looser one is tightened.
- Code that acts on your LinkedIn account is one module that a search never loads,
  and nothing is submitted without confirmation.
- API keys are refused in `applicant.toml`.
- Scraped text printed on the terminal is escaped like the log, so a posting
  cannot rewrite the list `apply` asks you to confirm; the log escapes C1
  control characters too.

## [0.1.0]

The state of `master` at `4441a79`, before the redesign.

[0.2.0]: https://github.com/toxicmender/Applicant/compare/4441a79...v0.2.0
