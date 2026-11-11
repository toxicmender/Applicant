# Contributing to Applicant

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

See the [research coverage matrix](research-coverage.md),
[Milestone 4 validation guide](milestone-4-validation.md),
[mutation testing report](mutation-report.md), and
[maintainer guide](maintainers.md) for the checks and release workflow.

Check local documentation links and anchors after moving or renaming sections.

```console
uv run --frozen python tools/check_doc_links.py
```

Use `--legacy-readme-ref REVISION` to check that every README fragment from a
previous Git revision still resolves. Use `--list-external` to collect external
URLs for a separate access check.
