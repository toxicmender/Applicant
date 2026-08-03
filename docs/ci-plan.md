# CI plan

Written against PR #2 (`company-reviews-submodule`, head `02a8b5b`). This is a plan,
not an implementation — nothing here is wired up yet.

---

## 1. Where the project actually stands

### PR #2 is three features, described as one

The branch carries three commits:

| Commit | What it does | Covered by the PR description? |
|---|---|---|
| `67124d1` | `src/utils/reviews/` — AmbitionBox + Glassdoor clients, `reviews` subcommand, move to `uv` | Yes, in detail |
| `9ebfa31` | Ports LinkedIn from Selenium to Playwright, adds `indeed.py`, `naukri.py`, `googlejobs.py` | No |
| `02a8b5b` | `jobsearch.py` + `jobs.py` — filters, salary parsing, apply flow, CSV application log | No |

2,557 insertions across 17 files, of which the description covers roughly the first
third. The "13/13 checks pass" verification refers only to the reviews clients; the
Selenium→Playwright port of the existing LinkedIn path and the three new boards have
no stated verification at all. That is the single largest risk in the PR — the port
rewrote the one code path that already worked.

**Recommendation:** split the branch, or at minimum rewrite the description to cover
all three commits and state what was and wasn't exercised for each. Reviewing 2.5k
lines of scraper against a description of one module is not a real review.

### The codebase itself

What is good, and makes CI worth doing:

- Clear separation. `jobs.py` holds shared primitives, one module per board, and
  `jobsearch.py` is a facade over them. Every board returns the same `Job`.
- A genuine seam for testing: `Indeed(client=...)` and `AmbitionBoxClient(client=...)`
  both accept an injected `httpx.Client`, and `Jobs(linkedin=...)` accepts an injected
  client. Roughly a third of the new code is testable without a network.
- Parsing is already isolated from fetching — `parse_salary`, `relative_to_iso`,
  `epoch_to_iso`, `JobFilter.matches`, `_to_job`, `_card_to_job`, `slugify`,
  `reviews_url`, `host_for`, `search_url` are all pure functions over strings.

What isn't there at all: no tests, no CI, no lint config, no type config, no dev
dependency group. This plan starts from zero.

---

## 2. What blocks CI today

These need fixing before any workflow can go green. They are small, but they are
hard blockers, not preferences.

### 2.1 `run.py` executes at import time — hard blocker

`src/run.py:190-200` parses `sys.argv` and calls `sys.exit()` at module level. There
is no `main()` and no `if __name__ == '__main__'` guard.

Consequences: pytest collection that imports `run` terminates the collector. Pyright
will analyse it, but no test can ever exercise the CLI in-process. Any test of
argument handling has to shell out to a subprocess.

**Fix:** wrap `src/run.py:143-200` in `def main(argv=None)` returning an exit code,
call it under a `__main__` guard. This also makes the bare-flag rewrite logic
(`argv = ['jobs'] + argv`) directly testable, which is exactly the compatibility
behaviour the PR promises and currently cannot prove.

### 2.2 Import layout needs explicit path configuration

Modules import as `from utils.linkedin import LinkedIn` — a top-level `utils` package
that only resolves when `src/` is on `sys.path`. `pyproject.toml` sets
`package = false`, so `uv sync` installs nothing and provides no path.

Every tool needs telling, separately:

```toml
[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]

[tool.pyright]
include = ["src", "tests"]
extraPaths = ["src"]
```

Ruff needs `src = ["src"]` for first-party import classification (`I` rules).

### 2.3 Type errors that exist today

Worth knowing before the first pyright run, so the failure isn't a surprise:

- `src/run.py:44-46` — `client` is bound to `AmbitionBoxClient()` on one branch and
  `GlassdoorClient(...)` on the other. Unrelated classes; pyright flags the
  reassignment. Fix by extracting a `_client(source, args)` helper with a declared
  return type, or a `Protocol` for the shared `fetch(company, max_reviews)` contract
  the docstring already claims exists.
- Bare generics throughout the dataclasses: `Job.flags: list`, `CompanyRating.reviews:
  list`, `rating_breakdown: dict`, `rating_distribution: dict`. Clean under `basic`,
  noisy under `strict`. Should become `list[str]`, `list[Review]`, `dict[str, float]`,
  `dict[int, int]`.
- `AmbitionBoxClient._get` (`ambitionbox.py:62-79`) can return `None` when
  `self.retries <= 0`; callers immediately do `response.status_code`.

### 2.4 Lint noise to expect

`except Exception` appears at `jobs.py:163`, `jobs.py:182`, `googlejobs.py`,
`linkedin.py` and elsewhere — mostly deliberate (a browser channel that isn't
installed, a stale Playwright locator). Ruff's `BLE001` will flag all of them. Do
**not** turn the rule off wholesale; add `# noqa: BLE001` with the reason at the sites
that are genuinely load-bearing, since several of those handlers are how the
multi-channel browser fallback works.

`jobsearch.py:_easy_apply` already carries a `# noqa: F401` on a deliberately unused
import — that import does nothing and should just go.

### 2.5 Everything else touches the network

Every `search()` and `fetch()` hits a live, bot-guarded site. GitHub-hosted runner IP
ranges are aggressively blocked by all five of these targets — LinkedIn, Indeed,
Naukri, Google Search and Glassdoor. **No CI job may ever hit a real site.** A
"smoke test" against production here is a permanently red build, not a canary.

Everything in this plan runs against recorded HTML/JSON fixtures or a local server.

### 2.6 Sleeps and destructors

`indeed.py:88` and `linkedin.py:96` call `time.sleep` inside pagination loops. Any
multi-page test pays that in wall clock. `Indeed(delay=0)` is already injectable;
LinkedIn's `1.0` is hardcoded and should become a constructor argument.

`LinkedIn.__del__` (`linkedin.py:333`) calls `close()` during GC. Under pytest this
runs at interpreter shutdown against possibly-torn-down Playwright state and produces
"Exception ignored in __del__" noise. Prefer explicit `close()` plus a context-manager
protocol; keep `__del__` as a backstop only.

---

## 3. The five CI jobs

One workflow, `.github/workflows/ci.yml`, on `push` and `pull_request`. All tooling
runs through `uvx` per the brief, with **pinned versions** — `uvx ruff@0.12.0` rather
than `uvx ruff`, so a tool release never turns an unrelated PR red.

Shared setup for every job: `actions/checkout`, then `astral-sh/setup-uv` with
`enable-cache: true`.

```
        ┌──────────┐  ┌──────────┐
        │   lint   │  │  types   │      (fast, parallel, no deps)
        └────┬─────┘  └────┬─────┘
             └──────┬──────┘
              ┌─────▼──────┐
              │ unit tests │              (matrix 3.10–3.13)
              └─────┬──────┘
              ┌─────▼──────┐
              │    e2e     │              (Playwright, local fixtures)
              └─────┬──────┘
              ┌─────▼──────┐
              │   status   │              (always(); aggregates + persists)
              └────────────┘
```

### Job 1 — `lint` (uvx ruff)

```yaml
- run: uvx ruff@0.12.0 check --output-format=github .
- run: uvx ruff@0.12.0 format --check .
```

Config in `pyproject.toml`:

```toml
[tool.ruff]
line-length = 100          # matches what the code already does
target-version = "py310"
src = ["src"]

[tool.ruff.lint]
select = ["E", "F", "W", "I", "UP", "B", "C4", "SIM", "RUF", "BLE", "PTH"]
ignore = [
    "E501",    # long lines are in docstrings and regexes; formatter owns wrapping
]

[tool.ruff.lint.per-file-ignores]
"tests/*" = ["S101"]       # asserts are the point
```

Two notes:

- **`ruff format` will reflow the entire codebase on first run.** The existing style
  uses `name = value` spacing in some call sites (`LinkedIn(path = args.driver,
  headless = args.Display)` in `run.py:22`) that the formatter will change. Land that
  reformat as one isolated commit *before* enabling the check, and add its SHA to
  `.git-blame-ignore-revs` so history stays readable.
- Introduce the rule set in two steps: land `E,F,W,I` first (near-clean today), then
  add `B,SIM,RUF,BLE,PTH` in a follow-up so the fixes are reviewable separately from
  the CI wiring.

### Job 2 — `types` (uvx pyright)

```yaml
- run: uv sync                        # pyright needs httpx/playwright stubs resolvable
- run: uvx pyright@1.1.400
```

```toml
[tool.pyright]
include = ["src", "tests"]
extraPaths = ["src"]
pythonVersion = "3.10"
venvPath = "."
venv = ".venv"
typeCheckingMode = "basic"
reportMissingImports = "error"
```

`uv sync` before pyright is not optional — without the venv, every `import httpx` and
`import playwright` is an unresolved-import error and the run is meaningless.

Start at `basic`. It should pass after the §2.3 fixes. Move to `standard` once the
dataclass generics are parameterised, and treat `strict` as a later goal for
`src/utils/` only — the CLI layer isn't worth it.

### Job 3 — `unit` (unittest + pytest)

The brief asks for both. They are not alternatives: **write tests as
`unittest.TestCase`, run them under pytest.** pytest collects `TestCase` subclasses
natively, so one suite satisfies both — `python -m unittest discover` works for a
contributor with nothing installed, and pytest gives the CI run its reporting,
`-x`/`-k`, and coverage.

```yaml
strategy:
  fail-fast: false
  matrix:
    python: ["3.10", "3.11", "3.12", "3.13"]
steps:
  - run: uv sync --python ${{ matrix.python }}
  - run: uvx --with-editable . pytest@8.3.0 tests/unit -v --junitxml=junit-${{ matrix.python }}.xml
  - run: uv run python -m unittest discover -s tests/unit -t .    # bare-stdlib path stays honest
```

3.10 is in the matrix because `requires-python = ">=3.10"` claims it. If nobody
actually runs 3.10, raise the floor instead of testing a version you don't support.

Proposed layout:

```
tests/
  fixtures/               # recorded HTML/JSON, checked in, trimmed
    indeed_mosaic.html
    linkedin_guest_cards.html
    ambitionbox_next_data.html
    ambitionbox_ldjson_only.html
    glassdoor_apollo.json
    googlejobs_card.txt
  unit/
    test_salary.py        # parse_salary — the highest-value target here
    test_filters.py       # JobFilter.matches, skip=, keep_unknown, flags
    test_dates.py         # relative_to_iso, epoch_to_iso
    test_storage.py       # save_jobs merge/dedup, ApplicationLog
    test_indeed.py        # host_for, _url, _results, _to_job
    test_linkedin.py      # _card_to_job, _load_state (Selenium→Playwright convert)
    test_googlejobs.py    # _to_job line classification
    test_naukri.py        # _slug, search_url
    test_reviews.py       # slugify, reviews_url, extractors, ld+json fallback
    test_cli.py           # argv rewriting — needs §2.1 first
```

Priority targets, in order of bugs-per-line-of-test:

1. **`parse_salary`** (`jobsearch.py:79-146`). The densest logic in the PR: currency
   detection, period normalisation, lakh/crore multipliers, the "unit only on the last
   number" range rule, and the year-lookalike guard. Table-driven test —
   `'2-2.5 Lacs PA'`, `'₹25K–₹40K a month'`, `'$30 an hour'`, `'Not disclosed'`,
   `'2024'`, `'£45,000 - £55,000 per annum'`. This function decides which jobs a user
   sees; a wrong multiplier is off by 100×.
2. **`JobFilter.matches`** with `skip=`. The docstring explains that re-checking
   location locally *throws away correct results*. That invariant deserves a test that
   fails loudly if the `skip` plumbing in `Jobs.search` (`jobsearch.py:340`) regresses.
3. **`ApplicationLog`** — the never-record-the-same-posting-twice guarantee, header
   written once, `utf-8-sig` BOM, stable column order. Cheap, pure filesystem.
4. **`save_jobs` / `_key`** — merge-across-runs and the Google-Jobs fallback key.
5. **The `_to_job` parsers**, each against one trimmed fixture. These are what break
   when a site redesigns, so the test's job is to fail *informatively* when the fixture
   goes stale.
6. **`LinkedIn._load_state`** — old Selenium `cookies.json` → Playwright storage_state.
   Backward compatibility the PR promises and nothing checks.

Network calls get an injected fake — `Indeed(client=httpx.Client(transport=httpx.MockTransport(...)))`
and the same for `AmbitionBoxClient`. No `responses`/`vcr` dependency needed; the
constructors already take the seam.

Coverage: report it, don't gate on it initially. A percentage threshold on a codebase
that is 60% browser driving just teaches people to write tests for the easy 40%. Gate
on `src/utils/jobsearch.py` and `src/utils/jobs.py` specifically once those are covered.

### Job 4 — `e2e` (Playwright)

This is where the plan diverges most from the obvious approach, so the reasoning matters.

**The five target sites cannot be reached from CI** (§2.5). An end-to-end job that
loads `linkedin.com` will be red permanently and everyone will learn to ignore it.

Instead, E2E means: *serve the recorded fixtures over local HTTP and drive the real
browser code path against them.* That exercises everything the unit tests can't —
Playwright launch, the channel fallback in `jobs.browser()`, `looks_blocked`,
navigation, scrolling, `page.on('response')` interception, card extraction from a live
DOM — with zero external dependencies.

```yaml
- run: uv sync
- run: uv run playwright install --with-deps chromium
- run: uvx --with-editable . pytest@8.3.0 tests/e2e -v --junitxml=junit-e2e.xml
- if: failure()
  uses: actions/upload-artifact@v4
  with:
    name: playwright-traces
    path: test-results/
```

`tests/e2e/conftest.py` starts a `http.server` thread on a random port serving
`tests/fixtures/pages/`, and tests point the clients at it:

| Test | Exercises |
|---|---|
| `test_browser_launches` | `jobs.browser()` yields a usable page and closes cleanly |
| `test_browser_channel_fallback` | `channels=('nonexistent', None)` still launches, error list is right |
| `test_looks_blocked` | Cloudflare-interstitial fixture → `True`; normal page → `False` |
| `test_naukri_intercept` | fixture page fires an XHR matching `/jobapi/v3/search`; `_capture` collects it |
| `test_naukri_dom_fallback` | same page with the XHR removed → falls back to parsing tuples |
| `test_googlejobs_scroll` | fixture lazy-loads cards on scroll; `_load_more` stall logic terminates |
| `test_indeed_browser_path` | HTTP 403 from the injected client → browser path → parses mosaic blob |
| `test_run_search_end_to_end` | `run.py search` subprocess against fixture host → writes `job_listing.json` |

Runner notes:

- `--with-deps` for system libraries; cache `~/.cache/ms-playwright` keyed on the
  Playwright version from `uv.lock`.
- Pin `channels=(None,)` in E2E. The production default tries Chrome then Edge then
  bundled (`jobs.py:CHANNELS`); on a runner that resolves unpredictably. Test the
  fallback *logic* explicitly instead, as above.
- `xvfb` is not needed — headless throughout. `--show` is a human affordance.
- Chromium only. The other engines are irrelevant; this project ships one browser.

**Separately, and not in CI: a scheduled contract check.** A nightly `workflow_dispatch`
+ `schedule` workflow that hits the real sites and *opens an issue* on parse failure —
never fails a PR. That is how you find out Indeed moved its mosaic blob, without
blocking someone's unrelated typo fix. Mark it `continue-on-error: true` and keep it in
a separate file so nobody confuses it for a gate.

### Job 5 — storing job and application status

The phrase reads two ways, and both are real work, so both are planned. **Reading A is
the CI job**; Reading B is a code change the CI job would then track.

#### Reading A — persist the status of the CI jobs themselves

A final job with `if: always()` that collects the four upstream results and makes them
durable and queryable:

```yaml
status:
  needs: [lint, types, unit, e2e]
  if: always()
  steps:
    - uses: actions/download-artifact@v4        # all junit-*.xml
    - run: uv run python tools/ci_status.py --out status.json
    - uses: actions/upload-artifact@v4          # status.json + merged junit
    - run: uv run python tools/ci_status.py --summary >> $GITHUB_STEP_SUMMARY
    - if: github.event_name == 'pull_request'
      # single sticky comment, edited in place — never a new comment per push
```

What it stores, per run: commit SHA, branch, PR number, timestamp, each job's
conclusion, per-suite pass/fail/skip counts from the JUnit XML, duration, and the
`flags` distribution from any fixture-parse tests (so a slow drift in fixture staleness
is visible before it becomes a break).

Where it stores it, cheapest first:

1. **Artifacts + job summary** — zero infrastructure, 90 days retention. Start here.
2. **A `ci-status` orphan branch** — the job appends one JSON line per run and pushes.
   Gives unlimited history and a trivially plottable file, no external service. Guard
   with `if: github.ref == 'refs/heads/master'` so PR runs don't write.
3. **GitHub Deployments/Checks API** if it ever needs to be queried by tooling.

`always()` matters: the point of this job is to record *failures*. Note that `needs`
with `always()` still runs when an upstream job is skipped, so `ci_status.py` must
treat `skipped` as distinct from `success` or a cancelled run will look green.

#### Reading B — the application's own job and application status store

Today the app persists two flat files: `job_listing.json` (merged on write, keyed by
`(source, id)`) and `applied_jobs.csv` (append-only, deduped on `(source, id)`, read
back in full on every write via `ApplicationLog.existing_keys`).

This works and shouldn't be replaced yet — the CSV's Google Sheets import is a real
feature (README documents it), and JSON is inspectable. But three limits are already
visible in the PR:

- `status` is write-once. There is no path from `needs_manual_apply` to `applied` after
  a human does it by hand, which is the *majority* case for Indeed/Naukri/Google.
- Nothing links `applied_jobs.csv` back to the company ratings from
  `company_reviews.json`. The reviews feature exists precisely to inform applying, and
  the two never meet.
- `existing_keys()` reads the whole CSV per `record()` call. Fine at hundreds of rows,
  quadratic in spirit.

Proposed direction, in order:

1. **A status vocabulary with transitions** — `found → filtered → queued → applied →
   acknowledged → interview → rejected → offer`, plus `needs_manual_apply` and `failed`
   as terminal-until-updated. Write it down before storing it.
2. **SQLite as the store** (`applicant.db`, stdlib, no dependency): tables `jobs`,
   `applications` (job_id, status, changed_at, note), `companies` (rating, count,
   fetched_at). Status becomes an event log, so history is preserved rather than
   overwritten.
3. **Keep CSV as an export, not the store** — `run.py export --csv` reproduces today's
   exact columns and BOM so the Sheets workflow is untouched. This is the migration's
   compatibility promise, and it's directly testable in Job 3.
4. **A `status` subcommand** — `run.py status --set applied --id <id>`, and
   `run.py status --list interview`, so the state machine is usable without editing a
   CSV by hand.

This is the natural next PR after CI lands, and it is the one that makes the
job-boards work and the reviews work into a single product rather than two scrapers.

---

## 4. Sequencing

Nothing here should land as one PR.

**Phase 0 — unblock (prerequisite, no CI yet)**
- `main()` + `__main__` guard in `run.py` (§2.1)
- path config for pytest/pyright/ruff in `pyproject.toml` (§2.2)
- `ruff format` sweep as one isolated commit + `.git-blame-ignore-revs`
- `[dependency-groups] dev` for local parity with the pinned `uvx` versions

**Phase 1 — lint + types**
Fast, no fixtures, catches the §2.3 issues. Both jobs green before anything else.

**Phase 2 — unit tests**
`parse_salary` and `JobFilter` first; they carry the most logic and no I/O. Add the
fixture-backed parsers as fixtures are recorded.

**Phase 3 — E2E**
Local fixture server, Playwright, artifacts on failure.

**Phase 4 — status job**
Reading A. Artifacts and job summary first; the `ci-status` branch only if the history
turns out to be wanted.

**Phase 5 — status store**
Reading B, as its own PR with its own review.

**Branch protection** should only require `lint`, `types` and `unit`. E2E can be
required later once it has a few weeks of proven stability; requiring a
browser-driving job from day one is how a team learns to merge with a red check.

---

## 5. Open question

Item 5 of the brief — "storing job and application status" — is planned both ways in
§3.5 because the phrasing supports both, and both are worth doing. If only one was
meant, Reading A (persisting CI job status) is the one that belongs in a CI workflow,
and Reading B should be tracked as a product issue instead.
