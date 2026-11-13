# CLI usage

## Usage

`uv run applicant --help` lists the commands. Add `--help` after any command or
subcommand for its arguments. In an environment where Applicant is installed,
`applicant` and `python -m applicant` use the same interface.

Install Applicant using the [README instructions](../README.md#installation).
Run the examples from the repository root. Save JSON snippets using the filenames
shown, and replace placeholders such as `RUN_ID` and `FEED_ID` with IDs returned
by earlier commands. Examples that use an input file assume it already exists.

| Workflow | Commands |
|---|---|
| [Job discovery and applications](#job-discovery-and-applications) | `search`, `jobs`, `apply`, `status` |
| [Providers and sessions](#providers-and-sessions) | `sources`, `setup` |
| [Company and job research](#company-and-job-research) | `reviews`, `financials`, `company-evidence`, `research`, `feed`, `analytics`, `report` |
| [Candidate ranking and feedback](#candidate-ranking-and-feedback) | `survey`, `rank`, `feedback`, `evaluate`, `personalize` |
| [Benchmarks and exchange rates](#benchmarks-and-exchange-rates) | `benchmark`, `rates` |
| [Database maintenance](#database-maintenance) | `db`, `store` |

### Try an offline research workflow

This walkthrough uses synthetic observations in a separate database. It imports
jobs, groups matching requisitions, and calculates technology demand using the
fixture's observation date.

```console
uv run applicant research import --database demo-research.db --from-file tests/fixtures/research/journey.json --dry-run
uv run applicant research import --database demo-research.db --from-file tests/fixtures/research/journey.json
uv run applicant feed collect --database demo-research.db --from-imports --output demo-feed.json
uv run applicant analytics technologies --database demo-research.db --as-of 2026-11-10 --timezone Asia/Kolkata --output demo-analytics.json
```

For a complete offline run using a temporary directory and producing JSON,
Markdown, and PDF, install the PDF extra and run
`uv run --extra pdf python tools/smoke_research.py`.

### Job discovery and applications

#### Public job search (`search`)

Search LinkedIn guest listings, Indeed, Naukri, or Google Jobs without signing in.
The default search selects all four boards and merges results into
`job_listing.json`. The `sources` catalog also lists providers available through
research feeds and imports.

```console
uv run applicant search "machine learning engineer" -l India -s linkedin indeed -n 25
uv run applicant search "software engineer" -l Bengaluru --title backend --title "machine learning" --experience 5 --posted-within 14
uv run applicant search "python developer" --min-salary 1200000 --currency INR --salary-basis strict
uv run applicant search "python engineer" -s linkedin --experience 5 --enrich --enrich-limit 10 --want 15
uv run applicant search --list-saved
uv run applicant search --saved ai-ml --title "nlp"
```

Repeat `--title` or `--company` for alternatives. Each title phrase requires all
its words. Experience is checked against a published range. Missing information
is flagged by default. `--strict-published` drops jobs missing fields their board
normally publishes, and `--strict` drops results that cannot be verified against
the requested filters.

`--limit` caps results per board before filtering. `--want` can pull additional
rounds to reach a target after filtering. `--enrich` reads a bounded number of
supported posting pages for missing experience details. `--show` opens supported
browser-backed searches visibly.

#### Signed-in LinkedIn recommendations (`jobs`)

Collect recommended jobs using your LinkedIn session. Set up the session first
if needed. `--overwrite` is the compatibility path for replacing an expired
session, and `--jobs` chooses the listing file.

```console
uv run applicant setup provider linkedin
uv run applicant jobs
uv run applicant jobs --show --jobs recommendations.json
uv run applicant jobs --overwrite
```

#### Applications (`apply`)

Filter a stored listing and start with a dry run. Dry runs record `would_apply`
rows in the selected CSV. A live run previews the batch and asks for confirmation.
`--yes` explicitly approves the batch for scripting.

```console
uv run applicant apply --input job_listing.json --title "machine learning" --dry-run --log preview.csv
uv run applicant apply --title "python" --min-salary 1200000 --currency INR
uv run applicant apply --title "python" --yes
```

Automated submissions use LinkedIn Easy Apply and a valid saved session.
External applications become `needs_manual_apply` entries with their links.
Cross-board duplicates are consolidated for application purposes. The default
`applied_jobs.csv` is a UTF-8 spreadsheet export with formula-like source text
escaped. Other row statuses include `applied` and `failed`.

#### Listing and application totals (`status`)

Review stored jobs by source and applications by status. `--json FILE` writes
the summary to a file.

```console
uv run applicant status
uv run applicant status --input job_listing.json --log applied_jobs.csv --json status.json
```

### Providers and sessions

#### Provider catalog and health (`sources`)

List capabilities and access modes, resolve aliases, and inspect health. Only
`probe` attempts live acquisition.

```console
uv run applicant sources list --json
uv run applicant sources show otta
uv run applicant sources health --provider wellfound
uv run applicant sources probe --provider wellfound
```

#### Provider authentication (`setup`)

Set up LinkedIn, Glassdoor, Crunchbase, or Tracxn in a visible browser. Session
status is local unless `--check` is supplied. For servers, import Playwright
storage state explicitly. `--verify` checks it headlessly, and `--replace`
authorizes replacement of an existing session.

```console
uv run applicant setup provider glassdoor
uv run applicant setup provider linkedin --reauth
uv run applicant setup status --provider linkedin
uv run applicant setup status --provider linkedin --check --database applicant.db
uv run applicant setup import-session --provider linkedin --from-file state.json --verify
uv run applicant setup import-session --provider linkedin --from-file cookies.json --format cookies --dry-run
```

Cookie-only imports omit local storage and report that limitation. Setup stores
sessions privately and authenticates the provider independently of data
collection. See [provider access and session behavior](research.md#authentication).

### Company and job research

#### Company reviews (`reviews`)

Fetch ratings, review counts, and review excerpts. AmbitionBox takes a company
slug. Glassdoor takes a reviews URL or an employer ID slug and can reuse a
session created with `setup provider glassdoor`.

```console
uv run applicant reviews tcs --source ambitionbox --max-reviews 40
uv run applicant reviews "https://www.glassdoor.com/Reviews/Google-Reviews-E9079.htm" --source glassdoor
uv run applicant reviews "https://www.glassdoor.com/Reviews/Google-Reviews-E9079.htm" --source glassdoor --login
```

#### Funding and financial history (`financials`)

Fetch company funding, rounds, valuation, and available revenue or headcount
context. Track selected companies or read their names from a job listing. API
access uses `CRUNCHBASE_API_KEY` or `TRACXN_API_KEY`. Browser-backed Tracxn
capture uses the company's profile URL and its configured session.

```console
uv run applicant financials zomato swiggy --source crunchbase --rounds
uv run applicant financials --from-jobs job_listing.json --source crunchbase
uv run applicant financials "https://tracxn.com/d/companies/COMPANY/__ENTITY_ID" --source tracxn --rounds
uv run applicant financials zomato --source crunchbase --login
```

For Tracxn company names without an API key, Applicant reuses a uniquely matched
saved result checked less than 30 days ago. Pass `--refresh` to bypass it.
Legacy records with no timezone offset stay stale until a successful refresh.
In a terminal, Applicant offers to open your saved session when it first needs a profile URL.
Set up that session with `uv run applicant setup provider tracxn` if needed.

```console
uv run applicant financials --from-jobs job_listing.json -s tracxn
```

Search in the browser and open the company profile. Press Enter in the terminal
to inspect it. Review the company heading, tab title, and URL, then type `yes`
to read the available financials. Use `back` to choose another result, `skip`
to move past a company, or `stop` to end interactive lookup. Applicant closes the
browser after each company and opens a fresh one for the next lookup that needs
it. Use the profile reader only where your account permits browser extraction.

If financials reports `financials_busy`, rerun after the other Applicant command
finishes. For `export_pending`, close applications using the JSON file and rerun
to publish the committed SQLite data. For `export_conflict`, review the reported
keys in your edited JSON before retrying. Keep the sibling lock file while any
process is using the export or profile. You can remove it after all users of
that path have stopped.

#### Verified company evidence for ranking (`company-evidence`)

Associate a provider company ID with an exact job version, capture or import
evidence, and export enriched job versions for a new rank run. Use the payload
examples in the [company evidence guide](company-evidence.md).

```console
uv run applicant company-evidence associate --from-file association.json --dry-run
uv run applicant company-evidence associate --from-file association.json
uv run applicant company-evidence capture --association ASSOCIATION_ID
uv run applicant company-evidence refresh --association ASSOCIATION_ID
uv run applicant company-evidence import --from-file company-snapshot.json --dry-run
uv run applicant company-evidence import --from-file company-snapshot.json
uv run applicant company-evidence show --association ASSOCIATION_ID --json
uv run applicant company-evidence show --snapshot SNAPSHOT_ID --json
uv run applicant company-evidence enrich --from-file selections.json --export enriched-jobs.json
```

#### Company identity, research imports, and reports (`research`)

Import dated compensation, culture, workplace policy, technology, job-party,
and corporate-event evidence. Edit the generated template with verified IDs and
source material before importing. Company reports distinguish direct employers
from recruiting parties and label relevant predecessor evidence as historical.

```console
uv run applicant research template --kind compensation --output evidence.json --export-schema research-schema.json
uv run applicant research import --from-file evidence.json --dry-run
uv run applicant research import --from-file evidence.json
uv run applicant research backfill-companies
uv run applicant research bridge-company --snapshot COMPANY_SNAPSHOT_ID --entity-id ENTITY_ID
uv run applicant research capture-profile --from-file profile-selection.json
```

For the offline walkthrough, save this as `selection.json`. For your own data,
use an entity ID from the research import.

```json
{"entity_ids":["startup"],"as_of":"2026-11-10","timezone":"Asia/Kolkata"}
```

```console
uv run applicant research report --database demo-research.db --selection selection.json --output demo-report.json
uv run applicant research report --database demo-research.db --selection selection.json --section compensation,policy,inherited-context --summary --format markdown --output demo-report.md
```

The selection can pin evidence and job versions. Dated employer schedules use
the applicant's timezone and preserve seasonal offset changes. See [research
templates, report sections, and freshness rules](research.md).

#### Cross-board feeds (`feed`)

Collect fresh observations from selected providers or build a feed from imports.
Feeds preserve source and syndication details. Similar names or titles alone
yield possible matches. Add `--require-all-sources` when partial acquisition
should return a failure exit code.

Save as `query.json` for live collection:

```json
{"providers":["weworkremotely"],"keywords":"Python","limit":25}
```

```console
uv run applicant feed collect --query query.json --output feed.json
uv run applicant feed collect --from-imports --output imported-feed.json
uv run applicant feed show --feed FEED_ID --format markdown --limit 20 --offset 0
uv run applicant feed refresh --feed FEED_ID --output refreshed-feed.json
```

Use the same `--database` for collecting and reading a feed. `refresh` creates a
new snapshot. See [feed matching and provider recovery](research.md#feed-collection-and-matching)
and the [manual live release checklist](research.md#live-release-checklist).

#### Technology demand and compensation (`analytics`)

Count distinct observed openings by required technology or stack. The snapshot
pins job versions, evidence, the observation window, and the metric policy.
Salary comparisons disclose sample size and use compatible annual gross base
pay. Supply an explicit FX snapshot to compare currencies.

```console
uv run applicant analytics technologies --as-of 2026-11-10 --timezone Asia/Kolkata --group-by technology --output analytics.json
uv run applicant analytics technologies --group-by stack --origin direct --currency EUR --fx-snapshot fx.json
uv run applicant analytics show --snapshot ANALYTICS_ID --format markdown
```

#### Report export and rendering (`report`)

Render a report previously exported as Applicant JSON or Applicant Markdown.
The PDF extra provides Latin text, accents, and currency symbols. Unsupported
scripts use reversible Unicode escapes by default. Use `--missing-glyph error`
for strict PDF preflight.

```console
uv run --extra pdf applicant report render --from-file demo-report.json --format markdown --output rendered.md --pdf report.pdf
uv run --extra pdf applicant report render --from-file rendered.md --pdf rendered.pdf
uv run --extra pdf applicant report render --from-file latin-report.json --pdf strict-report.pdf --missing-glyph error
```

### Candidate ranking and feedback

#### Candidate preferences (`survey`)

Create and edit a revisioned SQLite profile interactively, or use an editable
JSON document. Save the following as `preferences.json` for a new default profile.

```json
{
  "kind": "candidate_preferences",
  "schema_version": 1,
  "profile_id": "default",
  "expected_revision": 0,
  "preferences": {
    "experience_years": "5",
    "primary_discipline": "Software engineering",
    "work_country": "IN",
    "required_work_modes": ["Remote"]
  }
}
```

```console
uv run applicant survey edit
uv run applicant survey validate preferences.json
uv run applicant survey import preferences.json --dry-run
uv run applicant survey import preferences.json
uv run applicant survey show --revision 1 --json
uv run applicant survey export preferences-edit.json
```

Choose either `edit` or the initial `import` to create the profile. Export the
current revision before making subsequent file edits. Imports reject stale
revision numbers.

#### Ranking and replay (`rank`)

Create a run from a saved profile and the default listing, an explicit listing,
or enriched job versions. Save `provenance.json` with the retained source or
release identity and the corresponding `uv.lock` SHA-256. PowerShell can compute
the digest with `(Get-FileHash uv.lock -Algorithm SHA256).Hash.ToLowerInvariant()`.

```json
{
  "kind": "ranking_provenance",
  "schema_version": 1,
  "build_identity": "RETAINED_BUILD_ID",
  "lockfile_digest": "REPLACE_WITH_64_LOWERCASE_HEX_DIGITS"
}
```

```console
uv run applicant rank create --provenance provenance.json --offline
uv run applicant rank create --provenance provenance.json --input job_listing.json --top 10
uv run applicant rank create --provenance provenance.json --job-versions enriched-jobs.json --offline --json
uv run applicant rank list --profile default --json
uv run applicant rank show RUN_ID --all --json
uv run applicant rank explain RUN_ID FULL_RANKING_JOB_ID --json
uv run applicant rank compare BASE_RUN_ID CANDIDATE_RUN_ID --json
uv run applicant rank replay RUN_ID --provenance provenance.json
```

Reports show eligibility, fit, evidence confidence, and coverage separately.
Use full job IDs from `rank show` for explanations and feedback. Replay verifies
a completed engine run with its exact archived inputs and retained build
provenance. See [ranking input and replay details](milestone-3-plan.md).

#### Candidate judgments (`feedback`)

Record judgments against a completed run. Revision 0 creates the first event.
A dismissal requires a reason. Clearing a judgment uses the current revision
and omits reasons and importance.

```console
uv run applicant feedback record --run RUN_ID --job FULL_RANKING_JOB_ID --event-id review-1 --expected-revision 0 --decision shortlist
uv run applicant feedback record --run RUN_ID --job FULL_RANKING_JOB_ID --event-id review-2 --expected-revision 1 --decision dismiss --reason compensation
uv run applicant feedback list --run RUN_ID --history --json
uv run applicant feedback import --from-file feedback.json --dry-run
uv run applicant feedback import --from-file feedback.json
```

Batch imports use a `candidate_feedback_import` document with `schema_version: 1`
and an `events` array. See [feedback contracts and validation](milestone-4-delivery.md).

#### Evaluation (`evaluate`)

Measure descriptive precision and recall from explicit judgments. Unjudged
results stay unknown. Use a feedback sequence to reproduce a historical view.

```console
uv run applicant evaluate --run RUN_ID --top 20 --export evaluation.json
uv run applicant evaluate --run RUN_ID --feedback-through 20 --json
```

#### Weight suggestions (`personalize`)

Preview bounded weight changes from recorded importance directions, then accept
a suggestion explicitly to save a new profile revision.

```console
uv run applicant personalize suggest --run RUN_ID --json
uv run applicant personalize show --suggestion SUGGESTION_ID --json
uv run applicant personalize accept --suggestion SUGGESTION_ID --json
```

Suggestions require enough explicit support from eligible jobs. Use
`--importance compensation=more_important` or
`--importance culture=less_important` when recording the relevant judgment.

### Benchmarks and exchange rates

#### Wage benchmarks (`benchmark`)

Import wage cohorts or acquire them from Eurostat, BLS, or MoSPI. Fetch uses a
provider-specific query document. Comparisons use explicit job-to-cohort
selections and the ranking run's archived FX. See [benchmark schemas and
complete query examples](benchmark-contracts.md).

```console
uv run applicant benchmark import --from-file snapshot.json --dry-run
uv run applicant benchmark import --from-file snapshot.json
uv run applicant benchmark show --snapshot RELEASE_ID --json
uv run applicant benchmark compare --run RUN_ID --selections selections.json --export comparison.json
uv run applicant benchmark discover --provider eurostat --export eurostat-dimensions.json
uv run applicant benchmark discover --provider bls --export bls-dimensions.json
uv run applicant benchmark discover --provider mospi --export mospi-dimensions.json
uv run applicant benchmark fetch --query eurostat-query.json --export acquired-eurostat.json
uv run applicant benchmark fetch --query bls-query.json --export acquired-bls.json
uv run applicant benchmark fetch --query mospi-query.json --export acquired-mospi.json
```

#### Comparing pay across currencies (`rates`)

Search and apply filters annualize posted pay before comparing it with
`--min-salary` and `--currency`. Use `--salary-basis ppp` for purchasing power
(the default), `market` for exchange rates, or `strict` for same-currency
comparisons. Missing PPP factors can fall back to market rates with a
`ppp-unavailable` flag.

```console
uv run applicant rates
uv run applicant rates --refresh
uv run applicant rates --refresh --currency GBP SEK NZD
uv run applicant rates --refresh --force
```

The `rates` command inspects or refreshes PPP factors in the data directory.
For reproducible salary analysis, use the explicit FX inputs supported by
ranking and research analytics.

### Database maintenance

#### Schema upgrades (`db`)

Upgrade an existing SQLite database before using commands that require the
current schema.

```console
uv run applicant db upgrade --database applicant.db
```

#### WAL checkpoints (`store`)

Use a passive checkpoint for routine maintenance. Request `truncate` explicitly
to reclaim the WAL file after active readers and writers release it.

```console
uv run applicant store checkpoint --database applicant.db
uv run applicant store checkpoint --database applicant.db --mode truncate --json
```

## Configuration and storage

Settings resolve in this order, with later values taking precedence: built-in
defaults, `applicant.toml` (or `--config PATH`), environment variables, and CLI
flags. `--data-dir` or `APPLICANT_HOME` selects where relative input and output
paths resolve. A relative `data_dir` in TOML resolves from that file's directory.

```toml
data_dir = "data"
store = "sqlite"
timezone = "Asia/Kolkata"

[files]
listing = "job_listing.json"

[searches.ai-ml]
keywords = "machine learning engineer"
location = "India"
source = ["naukri", "linkedin"]
experience = 5
title = ["machine learning", "ml engineer"]
posted_within = 14
```

```console
uv run applicant --config applicant.toml search --saved ai-ml
uv run applicant --data-dir data --store files status
uv run applicant --data-dir data --no-log-file sources list --json
```

SQLite is the CLI default. `applicant.db` stores jobs and history, while
`job_listing.json` and `applied_jobs.csv` provide file interchange.
`--store files` supports legacy discovery, application, and company workflows
using JSON and CSV alone. Survey, ranking, and research collection use SQLite.
Use environment variables or the corresponding flags for API keys.

Results go to stdout and progress goes to stderr. Use `-q` for warnings and
failures, `-v` for diagnostics, or `-vv` for HTTP details. The CLI normally
writes a private per-run log under `logs/`. Override it with `--log-file PATH`
or disable it with `--no-log-file`.

| Exit code | Common meaning |
|---:|---|
| 0 | Success |
| 1 | No usable result or operation failed |
| 2 | Invalid usage, configuration, or input |
| 3 | Provider blocked or session setup required |
| 4 | Requested record or resource missing |
| 5 | Provider response could not be parsed |
| 6 | Authentication or API quota failure |
| 7 | Provider unreachable |
| 8 | Database busy or retryable database operation |
| 130 | Interrupted |

### Usage notes for scripting

Use `--dry-run` where supported to validate or preview a write. `--yes` is for
application submission and `personalize accept` saves a new preference revision.
Use dedicated output paths for exports and retain the snapshot or run ID when
you need to read a frozen result later.
