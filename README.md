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

No chromedriver step any more - everything runs on Playwright, which manages its own
browser. The `--driver` flag is still accepted but ignored.

> If you have Chrome or Edge installed, keep it. Naukri and Google both reject Playwright's
> bundled Chromium outright ("Access Denied"), so the scrapers try an installed Chrome, then
> Edge, then the bundled build.

## Searching job boards
`run.py search` pulls listings from LinkedIn, Indeed, Naukri and Google Jobs into
`job_listing.json`. **No account needed** - none of these use your login.

```
uv run python run.py search "python developer" -l India -n 25
uv run python run.py search "data engineer" -s linkedin indeed -n 50
```

- `-s/--source` picks any of `linkedin`, `indeed`, `naukri`, `googlejobs`, or `all` (default).
  A board that gets blocked is reported and skipped rather than losing the others' results.
- `--show` runs the browser visibly so you can clear a bot check yourself.
- Results are merged into the output file across runs rather than overwritten.

### Filters

```
uv run python run.py search "developer" -l India --title "senior python" \
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

Location and date are handed to the boards themselves where they support it
(LinkedIn and Indeed both filter by date server side), and applied locally otherwise.
`-n/--limit` is how many to pull from each board *before* filtering, so a tight
filter returns fewer than you asked for - raise it if you want more survivors.

**Unverifiable jobs are kept and flagged, not dropped.** Most postings state no
salary, and only Naukri publishes required experience, so those come back marked
`salary-unknown` / `experience-unknown` in the `flags` field. Pass `--strict` to drop
anything that could not actually be checked.

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
uv run python run.py apply --min-salary 1200000 --currency INR --dry-run
uv run python run.py apply --title "python"
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
1. Run `uv run python run.py -h` or `uv run python run.py --help` to see the full list of arguments supported
2. `uv run python run.py` without arguments it'll create 2 files in current directory by the name of `cookies.json` storing session cookies & `job_listing.json` for scraped jobs.

Job scraping also has its own subcommand, `run.py jobs`, which takes the same flags. Running
`run.py` with bare flags still means the job run, so existing invocations keep working.

## Company ratings
`run.py reviews` pulls a company's overall rating, its rating count and the pros/cons of
individual reviews from AmbitionBox and Glassdoor, writing them to `company_reviews.json`.

```
uv run python run.py reviews tcs -n 40
uv run python run.py reviews "https://www.glassdoor.com/Reviews/Google-Reviews-E9079.htm" -s glassdoor --login
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
from utils.jobsearch import Jobs, JobFilter

board = Jobs()
hits = board.search('python developer',
                    JobFilter(location='India', min_salary=1_200_000,
                              currency='INR', salary_basis='ppp',
                              experience=5, posted_within_days=7))
board.apply(hits, log='applied_jobs.csv', dry_run=True)
```

`Job` is a Pydantic model, so postings are validated as they are built - blank
strings become `None`, whitespace is stripped, and a year range is derived from
whatever the board said (`experience_text`) into `experience_min` / `experience_max`.

Individual boards work standalone too, and all four return the same `Job` objects:

```python
from utils.indeed import Indeed

for job in Indeed().search('python developer', 'remote', limit=10):
    print(job.title, job.company, job.location, job.salary)
```

`LinkedIn` additionally keeps the signed-in flows - `login()`, `restore_session()`,
`scrape_jobs()` for recommended jobs and `easy_apply()`. Sessions are stored as Playwright
storage state, and a `cookies.json` written by the older Selenium version is converted
automatically on read.

Company ratings are also importable:

```python
from utils.reviews import AmbitionBoxClient

rating = AmbitionBoxClient().fetch('tcs', max_reviews=40)
print(rating.overall_rating, rating.review_count)
print(rating.reviews[0].pros, rating.reviews[0].cons)
```

Both clients share the same `fetch(company, max_reviews)` signature and return a
`CompanyRating`, so callers never branch on the source.
