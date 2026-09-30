# Changelog

All notable changes to applicant. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/): while the major version is 0, a breaking
change bumps the minor version.

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
  itself.

### Fixed

- `master` was red while every check was green: a merge had dropped the CLI's
  last-resort error handler, and pytest stopped after 124 of 480 tests.
- CI now fails on a failing test or type error.
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
