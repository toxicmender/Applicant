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

The job boards are importable too, and all four return the same `Job` objects:

```python
from utils.indeed import Indeed
from utils.naukri import Naukri

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
