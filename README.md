# Applicant
It is a dynamic web scraper which gets the posted jobs which are applicable from within the site and don't redirect to another site. It then applies to them automatically with your stored (Latest) Resume.
> _**Note: Filter operation hasn't been implemented yet for jobs**_

## Setup
The project is managed with [uv](https://docs.astral.sh/uv/):

```
uv sync
uv run playwright install chromium
```

`requirements.txt` is kept in sync for anyone who would rather use `pip install -r requirements.txt`.

## Development

```
uv sync                       # includes the dev group: ruff, pyright, pytest
uv run pytest tests           # or: uv run python -m unittest discover -s tests -t .
uv run ruff check . && uv run ruff format .
uv run pyright
```

Tests are `unittest.TestCase` subclasses run under pytest, so both runners work and
neither is required. Nothing in the suite touches the network: the boards take an
injected `httpx` client, the parsers run against fixtures, and currency tests use
`JobFilter(rates=Rates(offline=True))` so no ECB or World Bank call is ever made.
Pass `rates=` yourself to keep a real search off the network too.

CI mirrors this in two workflows. `format` is the only one that writes - it runs
`ruff format` and pushes the result back to the branch. `ci` runs ruff, pyright and
the tests across Python 3.10-3.13, reports everything to the run summary, and stores
a `status.json` artifact; none of it gates a merge.

No chromedriver step any more - everything runs on Playwright, which manages its own
browser. The `--driver` flag is still accepted but ignored.

> If you have Chrome or Edge installed, keep it. Naukri and Google both reject Playwright's
> bundled Chromium outright ("Access Denied"), so the scrapers try an installed Chrome, then
> Edge, then the bundled build.

## Searching job boards
`applicant search` pulls listings from LinkedIn, Indeed, Naukri and Google Jobs into
`job_listing.json`. **No account needed** - none of these use your login.

```
uv run applicant search "python developer" -l India -n 25
uv run applicant search "data engineer" -s linkedin indeed -n 50
```

- `-s/--source` picks any of `linkedin`, `indeed`, `naukri`, `googlejobs`, or `all` (default).
  A board that gets blocked is reported and skipped rather than losing the others' results.
- `--show` runs the browser visibly so you can clear a bot check yourself.
- Results are merged into the output file across runs rather than overwritten.

### Filters

```
uv run applicant search "developer" -l India --title "senior python" \
    --company infosys --min-salary 1200000 --currency INR --posted-within 7
```

| Flag | Filters on |
|---|---|
| `-l/--location` | where the job is |
| `-t/--title` | words in the job title, any order |
| `-c/--company` | the hiring company |
| `--min-salary` + `--currency` | annual pay floor |
| `--salary-basis` | how to compare other currencies: `ppp`, `market` or `strict` |
| `-e/--experience YEARS` | years you have; keeps jobs asking for that much |
| `--posted-within DAYS` | how recently it was posted |
| `--strict` / `--strict-published` | what to do with what could not be checked |

Location and date are handed to the boards themselves where they support it
(LinkedIn and Indeed both filter by date server side), and applied locally otherwise.
`-n/--limit` is how many to pull from each board *before* filtering, so a tight
filter returns fewer than you asked for - raise it if you want more survivors.

**The boards differ in what they will tell you**, which decides what `-e` and
`--min-salary` can actually do:

| | LinkedIn | Indeed | Naukri | Google Jobs |
|---|---|---|---|---|
| states required experience | no | no | **yes** | no |
| states pay | no | yes | yes | sometimes |
| filters by date itself | yes | yes | no | no |

So `-e 5` is a real filter on Naukri and, on the other three, a request nobody
answered.

**Unverifiable jobs are kept and flagged, not dropped.** A flag ending `-unknown`
means the posting did not say; one ending `-unpublished` means its board never
says. So a LinkedIn result comes back `experience-unpublished` and a Naukri one
that hid its range comes back `experience-unknown`.

| | keeps | drops |
|---|---|---|
| *(default)* | everything, flagged | nothing |
| `--strict-published` | boards that never publish the field | postings that could have said and did not |
| `--strict` | only what was fully checked | both kinds of silence |

`--strict` with `-e` therefore discards every LinkedIn, Indeed and Google Jobs
result, since none of them publishes experience at all. `--strict-published` is
usually the one you want: it tightens Naukri without deleting the other three.

`--experience 5` keeps jobs whose stated range contains 5 years, so it excludes
roles wanting 0-2 years as well as ones wanting 8+. `5+ years` is treated as having
no upper bound, not as exactly five.

### Salaries across currencies

Pay is normalised to an annual figure first, so `2-2.5 Lacs PA`, `₹25K–₹40K a month`
and `$30 an hour` all compare properly. Pay in *another* currency is then converted,
and the basis matters a great deal:

| `--salary-basis` | ₹20,00,000 compared against a USD floor |
|---|---|
| `ppp` (default) | **$99,559** - what it is worth where it is earned |
| `market` | $20,978 - today's exchange rate |
| `strict` | not compared at all, flagged `salary-currency-mismatch` |

Purchasing power parity is the default because a market conversion makes every
Indian salary look small next to an American one, which is not a useful way to
choose a job. Exchange rates come from the ECB via frankfurter.dev; PPP conversion
factors from the World Bank indicator `PA.NUS.PPP`. Both are cached in
`.money_cache.json`.

**Nothing is ever guessed.** The World Bank API throttles hard, so factors are
fetched one country at a time and only when needed. When one cannot be had, the
comparison falls back to a market rate and says so with a `ppp-unavailable` flag
rather than inventing a number. Factors for India, Japan and the US ship built in;
the rest are fetched on first use.

## Applying

```
uv run applicant apply --min-salary 1200000 --currency INR --dry-run
uv run applicant apply --title "python"
```

Reads `job_listing.json`, keeps what matches the same filters as above, and appends
every one to `applied_jobs.csv`. `--dry-run` records what *would* happen without
submitting anything.

**Only LinkedIn Easy Apply is actually automated.** Indeed, Naukri and Google Jobs
hand off to each employer's own form, which differs every time, so those are logged
as `needs_manual_apply` with their url - the CSV doubles as your worklist. Multi-step
LinkedIn forms are left open rather than answered with guesses.

### The CSV

`applied_jobs.csv` appends across runs and never records the same posting twice. It
is written UTF-8 with a BOM and a stable column order, so **Google Sheets imports it
cleanly** via *File > Import > Upload* (currency symbols survive). Columns:

`applied_at`, `status`, `source`, `id`, `title`, `company`, `location`, `salary`,
`salary_annual_low`, `salary_annual_high`, `currency`, `experience_min`,
`experience_max`, `posted`, `url`, `flags`, `note`

`status` is one of `applied`, `needs_manual_apply`, `would_apply` (dry run) or `failed`.

## Comparing pay across currencies

`--min-salary` needs a `--currency`, and most postings are priced in another one.
`--salary-basis` decides how they are compared:

| basis | ₹20,00,000 against a USD floor | when to use it |
|---|---|---|
| `ppp` (default) | ~$99,600 | "which of these is the better job" |
| `market` | ~$21,000 | "what is this worth in my currency" |
| `strict` | not compared, flagged | when you would rather judge it yourself |

PPP factors come from the World Bank indicator `PA.NUS.PPP` and are cached in
`src/applicant/ppp_factors.json`, which is checked in so a fresh clone starts
with whatever is already known.

```
uv run applicant rates                          # what is cached right now
uv run applicant rates --refresh                # fetch everything missing
uv run applicant rates --refresh -c GBP SEK NZD # just these
uv run applicant rates --refresh --force        # re-fetch what is already there
```

**The file ships with only USD, INR and JPY.** The World Bank API throttles
hard - roughly one country per attempt - so the rest are fetched on demand and
cached as you use them, or all at once with `--refresh`. Run it once and commit
the result so nobody else has to.

Nothing is ever written from memory. A factor that cannot be retrieved is
reported and left out, and a comparison needing it falls back to a market rate
flagged `ppp-unavailable` rather than being handed an invented number.

Two things worth knowing before reading a converted figure: `EUR` maps to the
euro area aggregate, which is coarser than a single country, and `TWD` has no
factor at all because Taiwan is not a World Bank member.

### Checking where things stand

```
uv run applicant status
uv run applicant status --json status.json
```

Reads both files and reports how many jobs are stored per board and how many
applications sit in each status. `--json` writes the same summary as a machine
readable file.

How each board is reached, since they differ a lot:

| Board | Needs a browser | How the data is read |
|---|---|---|
| LinkedIn | no | its guest endpoint serves job cards to logged out clients |
| Indeed | no (browser as fallback) | the `mosaic-provider-jobcards` JSON blob |
| Naukri | yes | its own search API call is intercepted; falls back to the rendered tuples |
| Google Jobs | yes | the `udm=8` Jobs tab, read structurally |

Google Jobs is the most fragile of the four: it is Google Search, its CSS classes are
obfuscated and rotate, and it gives no posting URL - applications route back to the
originating board, which is reported as `via`.

## Usage
`uv run applicant -h` lists everything. `python -m applicant` works identically, and is
what to use without `uv`.

1. `uv run applicant --help` for the full list of arguments
2. `uv run applicant` without arguments creates two files in the current directory:
   `cookies.json` for the session and `job_listing.json` for the scraped jobs

Job scraping also has its own subcommand, `applicant jobs`, which takes the same flags.
Running `applicant` with bare flags still means the job run, so invocations documented
before subcommands existed keep working.

## Where things are

```
src/applicant/
  cli.py         argument parsing and the subcommand handlers
  __main__.py    python -m applicant
  models.py      the Job dataclass and the shared error types
  dates.py       relative and epoch posting dates -> ISO
  salary.py      reading pay off a posting, normalised to an annual figure
  browser.py     launching Playwright in a way the boards accept
  filters.py     JobFilter, and the flags saying what could not be checked
  storage.py     job_listing.json and applied_jobs.csv
  search.py      the facade over boards, filters and storage
  boards/        one module per job board, all returning Job
  money.py       FX and PPP factors, for comparing pay across currencies
  ppp_factors.json  the checked in PPP table, filled by `applicant rates --refresh`
  reviews/       company ratings from AmbitionBox and Glassdoor
tests/           unittest.TestCase suites, run under pytest
```

## Company ratings
`applicant reviews` pulls a company's overall rating, its rating count and the pros/cons of
individual reviews from AmbitionBox and Glassdoor, writing them to `company_reviews.json`.

```
uv run applicant reviews tcs -n 40
uv run applicant reviews "https://www.glassdoor.com/Reviews/Google-Reviews-E9079.htm" -s glassdoor --login
```

- `-s/--source` picks `ambitionbox` (default), `glassdoor` or `both`. With `both`, a source
  that fails is reported and skipped rather than losing the other one's results.
- AmbitionBox takes a company slug (`tcs`, `tata-consultancy`) and needs no browser.
- **Glassdoor takes a reviews url or a `Google-E9079` style slug with employer id.** Its
  company search sits behind the same bot check, so plain names cannot be resolved.
- Glassdoor is behind Cloudflare, so the first run needs `--login`: a visible browser opens,
  you clear the check and sign in once, and the profile in `.gd_profile/` is reused
  headlessly afterwards. Without a warmed profile the run reports a bot check and stops
  instead of returning empty results.

The whole thing is importable, with `Jobs` as the front door:

```python
from applicant.search import Jobs
from applicant.filters import JobFilter

board = Jobs()
hits = board.search(
    'python developer',
    JobFilter(
        location='India',
        min_salary=1_200_000,
        currency='INR',
        salary_basis='ppp',
        experience=5,
        posted_within_days=7,
    ),
)
board.apply(hits, log='applied_jobs.csv', dry_run=True)
```

`Job` is a Pydantic model, so postings are validated as they are built - blank
strings become `None`, whitespace is stripped, and a year range is derived from
whatever the board said (`experience_text`) into `experience_min` / `experience_max`.

Individual boards work standalone too, and all four return the same `Job` objects:

```python
from applicant.boards.indeed import Indeed

for job in Indeed().search('python developer', 'remote', limit=10):
    print(job.title, job.company, job.location, job.salary)
```

`LinkedIn` additionally keeps the signed-in flows - `login()`, `restore_session()`,
`scrape_jobs()` for recommended jobs and `easy_apply()`. Sessions are stored as Playwright
storage state, and a `cookies.json` written by the older Selenium version is converted
automatically on read.

Company ratings are also importable:

```python
from applicant.reviews import AmbitionBoxClient

rating = AmbitionBoxClient().fetch('tcs', max_reviews=40)
print(rating.overall_rating, rating.review_count)
print(rating.reviews[0].pros, rating.reviews[0].cons)
```

Both clients share the same `fetch(company, max_reviews)` signature and return a
`CompanyRating`, so callers never branch on the source.
