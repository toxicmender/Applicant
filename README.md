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

## Usage
1. Download Google's [Chromium Drivers](https://sites.google.com/a/chromium.org/chromedriver/downloads) & either add to Path or put it in the `src/` directory
2. Run `uv run python run.py -h` or `uv run python run.py --help` to see the full list of arguments supported
3. `uv run python run.py` without arguments it'll create 2 files in current directory by the name of `cookies.json` storing session cookies & `job_listing.json` for scraped jobs.

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

It is also importable:

```python
from utils.reviews import AmbitionBoxClient

rating = AmbitionBoxClient().fetch('tcs', max_reviews=40)
print(rating.overall_rating, rating.review_count)
print(rating.reviews[0].pros, rating.reviews[0].cons)
```

Both clients share the same `fetch(company, max_reviews)` signature and return a
`CompanyRating`, so callers never branch on the source.
