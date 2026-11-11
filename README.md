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

See the [Python API guide](docs/python-api.md) for embedding Applicant.

<a name="job-discovery-and-application-previews"></a>
- [Job discovery and application previews](docs/python-api.md#job-discovery-and-application-previews)

<a name="company-reviews-and-financials"></a>
- [Company reviews and financials](docs/python-api.md#company-reviews-and-financials)

<a name="ranking-history"></a>
- [Ranking history](docs/python-api.md#ranking-history)

<a name="ranking-compensation"></a>
- [Ranking compensation](docs/python-api.md#ranking-compensation)

<a name="ranking-eligibility"></a>
- [Ranking eligibility](docs/python-api.md#ranking-eligibility)

<a name="ranking-scores-and-views"></a>
- [Ranking scores and views](docs/python-api.md#ranking-scores-and-views)

<a name="complete-ranking-runs-and-offline-replay"></a>
- [Complete ranking runs and offline replay](docs/python-api.md#complete-ranking-runs-and-offline-replay)

## Repository layout

See the [module map](docs/contributing.md#repository-layout).

## Development

See the [contributor guide](docs/contributing.md#development) for development and validation commands.
