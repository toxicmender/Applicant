# Applicant

Applicant is a command-line application for finding jobs, tracking applications,
and researching employers. It also provides Python APIs for embedding these
workflows in other tools.

- Search job boards, compare listings, and preview applications.
- Research compensation, culture, and work arrangements with source evidence.
- Rank jobs against your preferences and replay saved rankings.
- Analyze technology demand and export reports as JSON, Markdown, or PDF.

**Terminal preview** · selected output columns from the synthetic quick start below.

```text
$ uv run applicant analytics technologies --database demo-research.db --as-of 2026-11-10 --timezone Asia/Kolkata --format markdown

# Technology Analytics

Metric policy: research-analytics-2

| Technology / stack | Observed openings | Origins |
| --- | --- | --- |
| python | 6 | {"agency":0,"direct":6,"unknown":0} |
```

## Contents

- [Installation](#installation)
- [Quick start](#quick-start)
- [Usage](#usage)
- [Configuration and storage](#configuration-and-storage)
- [Troubleshooting](#troubleshooting)
- [Documentation](#documentation)
- [Development](#development)

## Installation

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then clone
Applicant and install its locked dependencies. Applicant supports Python 3.10
and later.

```console
git clone --branch codex/cli-0.3.0 https://github.com/toxicmender/Applicant.git
cd Applicant
uv sync --locked
uv run applicant --version
```

Install Chromium for browser-backed workflows such as interactive login.
HTTP-only and offline workflows can skip this step.

```console
uv run playwright install chromium
```

For PDF exports, install the optional `pdf` extra and use `uv run --extra pdf`
when running PDF commands.

```console
uv sync --locked --extra pdf
```

From an environment where Applicant is already installed, `applicant` and
`python -m applicant` expose the same CLI.

## Quick start

### Try an offline research workflow

From the repository root, import the synthetic fixture into a demo database,
build a feed from those imports, and calculate technology demand on the fixture's
fixed date. Use an unused database filename for a clean demo.

```console
uv run applicant research import --database demo-research.db --from-file tests/fixtures/research/journey.json --dry-run
uv run applicant research import --database demo-research.db --from-file tests/fixtures/research/journey.json
uv run applicant feed collect --database demo-research.db --from-imports --output demo-feed.json
uv run applicant analytics technologies --database demo-research.db --as-of 2026-11-10 --timezone Asia/Kolkata --format markdown --output demo-analytics.md
```

Expect six distinct observed openings requiring Python. `demo-feed.json` holds
the matched listings and source observations. `demo-analytics.md` holds the
technology table and its saved snapshot provenance. This walkthrough needs no
provider account or browser.

To render that Applicant Markdown report as PDF, use the optional extra.

```console
uv run --extra pdf applicant report render --from-file demo-analytics.md --pdf demo-analytics.pdf
```

### Embed a search in Python

This separate example performs a live public LinkedIn search and requires
network access. The context manager handles client cleanup.

```python
from applicant.search import Jobs

with Jobs(sources=['linkedin']) as boards:
    jobs = boards.search('python developer')
```

## Usage

Run `uv run applicant --help` to list commands. Add `--help` after any
subcommand to see its options. The [CLI guide](docs/cli-usage.md#usage)
covers every command family and execution path.

### Job discovery and applications

Search public job boards, collect signed-in LinkedIn recommendations, preview
applications, and track outcomes. Public search and signed-in recommendations
use different access paths. Start with a preview before submitting anything.

```console
uv run applicant search "machine learning engineer" -l India -s linkedin indeed -n 25
uv run applicant apply --input job_listing.json --title "machine learning" --dry-run --log preview.csv
uv run applicant status --input job_listing.json --log preview.csv
```

See the [complete examples](docs/cli-usage.md#job-discovery-and-applications).

- <a name="public-job-search-search"></a>[Public job search (`search`)](docs/cli-usage.md#public-job-search-search)
- <a name="signed-in-linkedin-recommendations-jobs"></a>[Signed-in LinkedIn recommendations (`jobs`)](docs/cli-usage.md#signed-in-linkedin-recommendations-jobs)
- <a name="applications-apply"></a>[Applications (`apply`)](docs/cli-usage.md#applications-apply)
- <a name="listing-and-application-totals-status"></a>[Listing and application totals (`status`)](docs/cli-usage.md#listing-and-application-totals-status)

### Providers and sessions

Check which providers support live access, sessions, or imports. Sign in
through a visible browser when required. Servers can import Playwright storage
state explicitly.

```console
uv run applicant sources list
uv run applicant setup provider linkedin
uv run applicant setup status --provider linkedin
```

See the [complete examples](docs/cli-usage.md#providers-and-sessions).

- <a name="provider-catalog-and-health-sources"></a>[Provider catalog and health (`sources`)](docs/cli-usage.md#provider-catalog-and-health-sources)
- <a name="provider-authentication-setup"></a>[Provider authentication (`setup`)](docs/cli-usage.md#provider-authentication-setup)

### Company and job research

Collect company reviews and funding, record compensation and workplace
evidence, and analyze technologies from a saved job snapshot. Reports retain
source information and distinguish recruiters from hiring employers.

```console
uv run applicant research template --kind compensation --output evidence.json
uv run applicant research import --from-file evidence.json --dry-run
```

Fill in the template with sourced values and verified IDs before running the
validation command. The offline quick start supplies a complete example.

See the [complete examples](docs/cli-usage.md#company-and-job-research).

- <a name="company-reviews-reviews"></a>[Company reviews (`reviews`)](docs/cli-usage.md#company-reviews-reviews)
- <a name="funding-and-financial-history-financials"></a>[Funding and financial history (`financials`)](docs/cli-usage.md#funding-and-financial-history-financials)
- <a name="verified-company-evidence-for-ranking-company-evidence"></a>[Verified company evidence for ranking (`company-evidence`)](docs/cli-usage.md#verified-company-evidence-for-ranking-company-evidence)
- <a name="company-identity-research-imports-and-reports-research"></a>[Company identity, research imports, and reports (`research`)](docs/cli-usage.md#company-identity-research-imports-and-reports-research)
- <a name="cross-board-feeds-feed"></a>[Cross-board feeds (`feed`)](docs/cli-usage.md#cross-board-feeds-feed)
- <a name="technology-demand-and-compensation-analytics"></a>[Technology demand and compensation (`analytics`)](docs/cli-usage.md#technology-demand-and-compensation-analytics)
- <a name="report-export-and-rendering-report"></a>[Report export and rendering (`report`)](docs/cli-usage.md#report-export-and-rendering-report)

### Candidate ranking and feedback

Save your preferences, rank job versions, and record judgments against a run.
Replay uses the archived inputs. Weight suggestions require explicit acceptance
to change the saved profile.

```console
uv run applicant survey edit
uv run applicant rank list --profile default
```

The ranking guide explains how to create a run and its provenance file.

See the [complete examples](docs/cli-usage.md#candidate-ranking-and-feedback).

- <a name="candidate-preferences-survey"></a>[Candidate preferences (`survey`)](docs/cli-usage.md#candidate-preferences-survey)
- <a name="ranking-and-replay-rank"></a>[Ranking and replay (`rank`)](docs/cli-usage.md#ranking-and-replay-rank)
- <a name="candidate-judgments-feedback"></a>[Candidate judgments (`feedback`)](docs/cli-usage.md#candidate-judgments-feedback)
- <a name="evaluation-evaluate"></a>[Evaluation (`evaluate`)](docs/cli-usage.md#evaluation-evaluate)
- <a name="weight-suggestions-personalize"></a>[Weight suggestions (`personalize`)](docs/cli-usage.md#weight-suggestions-personalize)

### Benchmarks and exchange rates

Compare job pay with selected wage cohorts. Use a saved exchange-rate snapshot
to make cross-currency comparisons reproducible.

```console
uv run applicant benchmark discover --provider eurostat --export eurostat-dimensions.json
```

This example queries a live provider. The guide also covers offline imports.

See the [complete examples](docs/cli-usage.md#benchmarks-and-exchange-rates).

- <a name="wage-benchmarks-benchmark"></a>[Wage benchmarks (`benchmark`)](docs/cli-usage.md#wage-benchmarks-benchmark)
- <a name="comparing-pay-across-currencies-rates"></a>[Comparing pay across currencies (`rates`)](docs/cli-usage.md#comparing-pay-across-currencies-rates)

### Database maintenance

Upgrade an existing database and inspect WAL checkpoint progress. A passive
checkpoint can complete only partially while readers are active.

```console
uv run applicant store checkpoint --database applicant.db --mode passive --json
```

See the [complete examples](docs/cli-usage.md#database-maintenance).

- <a name="schema-upgrades-db"></a>[Schema upgrades (`db`)](docs/cli-usage.md#schema-upgrades-db)
- <a name="wal-checkpoints-store"></a>[WAL checkpoints (`store`)](docs/cli-usage.md#wal-checkpoints-store)

## Configuration and storage

The CLI defaults to SQLite and stores job and research history in
`applicant.db`. Use `job_listing.json` and `applied_jobs.csv` to exchange
data with other tools. Use `--data-dir` to keep runs in a dedicated directory.
File-only mode supports legacy workflows. Feeds, ranking, and research snapshots
use SQLite.

Provider sessions live in private files. The setup commands inspect or replace
them without collecting jobs. See the
[configuration guide](docs/cli-usage.md#configuration-and-storage) for TOML,
saved searches, logging, and environment variables.

<a name="usage-notes-for-scripting"></a>
- [Usage notes for scripting](docs/cli-usage.md#usage-notes-for-scripting)

## Troubleshooting

| Symptom | Next step |
| --- | --- |
| Browser executable missing | Run `uv run playwright install chromium`. |
| Session expired or provider returns `needs_setup` | Run `uv run applicant setup provider PROVIDER --reauth`, then check status. |
| Provider blocked or cooling down | Inspect `sources health` and use imports while live access is unavailable. |
| PDF support unavailable | Run the export with `uv run --extra pdf`. |
| PDF contains Unicode escapes | PDF supports Latin text and selected symbols. Use JSON or Markdown for original complex-script text. |

See the [research guide](docs/research.md) for provider recovery and the
[release checklist](docs/research.md#live-release-checklist) for live
cross-board verification. The synthetic demo exercises fixture matching.

## Documentation

- [Complete CLI usage](docs/cli-usage.md)
- [Research, feeds, analytics, and reports](docs/research.md)
- [Company evidence for ranking](docs/company-evidence.md)
- [Benchmark inputs and queries](docs/benchmark-contracts.md)

## Python API

See the [Python API guide](docs/python-api.md) for embedding Applicant.

- <a name="job-discovery-and-application-previews"></a>[Job discovery and application previews](docs/python-api.md#job-discovery-and-application-previews)
- <a name="company-reviews-and-financials"></a>[Company reviews and financials](docs/python-api.md#company-reviews-and-financials)
- <a name="ranking-history"></a>[Ranking history](docs/python-api.md#ranking-history)
- <a name="ranking-compensation"></a>[Ranking compensation](docs/python-api.md#ranking-compensation)
- <a name="ranking-eligibility"></a>[Ranking eligibility](docs/python-api.md#ranking-eligibility)
- <a name="ranking-scores-and-views"></a>[Ranking scores and views](docs/python-api.md#ranking-scores-and-views)
- <a name="complete-ranking-runs-and-offline-replay"></a>[Complete ranking runs and offline replay](docs/python-api.md#complete-ranking-runs-and-offline-replay)

## Repository layout

See the [module map](docs/contributing.md#repository-layout) for package responsibilities.

## Development

```console
uv sync --locked --extra pdf
uv run --frozen pytest tests
```

See the [contributor guide](docs/contributing.md#development) for linting,
coverage, and installed-package smoke tests.

## License

Applicant is distributed under the [MIT license](LICENSE).
