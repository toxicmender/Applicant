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
uv run pyright                # CI also runs: uv run pyright --pythonversion 3.12
```

Tests are `unittest.TestCase` subclasses run under pytest, so both runners work and
neither is required. Nothing in the suite touches the network: the boards take an
injected `httpx` client, the parsers run against fixtures, and currency tests use
`JobFilter(rates=Rates(offline=True))` so no ECB or World Bank call is ever made.
Pass `rates=` yourself to keep a real search off the network too.

CI mirrors this in two workflows. `format` is the only one that writes - it runs
`ruff format` and pushes the result back to the branch. `ci` runs ruff, type checks
with pyright against both Python 3.10 (the floor) and 3.12, runs the tests across
Python 3.10-3.13, reports everything to the run summary, and stores a `status.json`
artifact. A failing test or type error fails the run (its `status` job is the one to
mark as required); lint findings and format drift are reported, never blocking.

No chromedriver step any more - everything runs on Playwright, which manages its own
browser.

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

**`--title` and `--company` may be repeated**, and a job needs to match only one
of them. A search worth running usually spans more than one way of naming the
same role:

```
uv run applicant search "AI ML engineer" -l India -e 3 \
    -t ai -t ml -t "machine learning" -t "data scientist"
```

Each value keeps its own rule - every word of it must appear, in any order - so
`-t "machine learning"` still means both words.

| Flag | Filters on |
|---|---|
| `-l/--location` | where the job is |
| `-t/--title` | words in the job title, any order; repeat for any-of |
| `-c/--company` | the hiring company; repeatable too |
| `--min-salary` + `--currency` | annual pay floor |
| `--salary-basis` | how to compare other currencies: `ppp`, `market` or `strict` |
| `-e/--experience YEARS` | years you have; keeps jobs asking for that much |
| `--posted-within DAYS` | how recently it was posted |
| `--strict` / `--strict-published` | what to do with what could not be checked |

Location and date are handed to the boards themselves where they support it
(LinkedIn and Indeed both filter by date server side), and applied locally otherwise.

**A country filter understands its cities.** Boards answer `-l India` with bare
city names, so `apply -l India` over a stored file checks "India" against
"Bengaluru, Karnataka" itself and keeps it. A posting somewhere we can name is
dropped; one we cannot place at all - "Remote", a town in no table, or no location
given - is kept and flagged `location-unverified` (and dropped under `--strict`). The same table means `-l Bengaluru` reaches
Indeed's Indian site rather than its US one.
`-n/--limit` is how many to pull from each board *before* filtering, so a tight
filter returns fewer than you asked for - raise it if you want more survivors.

`--want N` asks for the number you actually have in mind: each board is re-read
with a doubled pull until N jobs survive the filter, it runs out, or
`--max-rounds` (default 4) is reached. It costs requests - the boards page from
the top and Google Jobs is a scrolling list, so a second round re-reads what the
first one saw - which is why it is off unless asked for.

**The boards differ in what they will tell you**, which decides what `-e` and
`--min-salary` can actually do:

| | LinkedIn | Indeed | Naukri | Google Jobs |
|---|---|---|---|---|
| states required experience | no | no | **yes** | no |
| states pay | no | yes | yes | sometimes |
| filters by date itself | yes | yes | no | no |

So `-e 5` is a real filter on Naukri and, on the other three, a request nobody
answered.

**`--enrich` reads the postings themselves** when a search card cannot answer an
`-e` filter. It fetches at most `--enrich-limit` postings (default 25), only ones
that survived every other filter, and only from a board with a readable posting
page - today that is LinkedIn, whose guest pages need no account and no browser.
What it finds is recorded as `experience-enriched` with the phrase the posting
used, so a number in the CSV can always be traced back. Experience only: a salary
read out of free prose is as likely to be a relocation allowance as a wage.

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

Pay is normalised to an annual figure, and pay in another currency is compared by
purchasing power by default. The details, and the other bases, are in
[Comparing pay across currencies](#comparing-pay-across-currencies).

## Applying

```
uv run applicant apply --min-salary 1200000 --currency INR --dry-run
uv run applicant apply --title "python"          # lists what it will send, asks first
uv run applicant apply --title "python" --yes    # no question: for a script
```

Reads `job_listing.json`, keeps what matches the same filters as above, and appends
every one to `applied_jobs.csv`. `--dry-run` records what *would* happen without
submitting anything - and without opening a browser or signing in.

**Nothing is sent in your name without a yes.** Without `--dry-run`, `apply` lists
every application it is about to submit and waits for you to type `yes`. Any other
answer submits nothing, and so does running with no terminal to ask on (a cron job,
a pipe) unless you pass `--yes`. Declined postings are left out of the log, so a
later run can still apply to them.

**Only LinkedIn Easy Apply is actually automated.** Indeed, Naukri and Google Jobs
hand off to each employer's own form, which differs every time, so those are logged
as `needs_manual_apply` with their url - the CSV doubles as your worklist. Multi-step
LinkedIn forms are left open rather than answered with guesses.

### The CSV

**One job on three boards is one application.** The same posting found on
LinkedIn, Indeed and Google Jobs is stored three times - each board carries
different fields, and only some carry a url - but applying collapses them to the
copy you can actually act on: Easy Apply over a plain url, a url over a Google
Jobs row that has none. The boards that lost are recorded on the survivor as
`also-on-indeed` flags, and a later run will not apply again through another
board. Two postings from the *same* board stay two postings; there its own id is
the authority.

`applied_jobs.csv` appends across runs and never records the same posting twice. It
is written UTF-8 with a BOM and a stable column order, so **Google Sheets imports it
cleanly** via *File > Import > Upload* (currency symbols survive). Columns:

`applied_at`, `status`, `source`, `id`, `title`, `company`, `location`, `salary`,
`salary_annual_low`, `salary_annual_high`, `currency`, `experience_min`,
`experience_max`, `posted`, `url`, `flags`, `note`

`status` is one of `applied`, `needs_manual_apply`, `would_apply` (dry run) or `failed`.

**Formulas in scraped text are neutralised.** Titles, companies and locations come
from job boards, and a spreadsheet runs any cell starting with `=`, `+`, `-` or `@` as a
formula - a posting could use that to send your sheet's contents elsewhere
([CSV Injection](https://owasp.org/www-community/attacks/CSV_Injection)). Such cells
are written with a leading apostrophe, so `=HYPERLINK(...)` arrives as the text
`'=HYPERLINK(...)`, and every cell is quoted. Reading the log back through
`ApplicationLog.rows()` strips the apostrophe again.

> Excel can drop that protection if you save the file from Excel and open it again, as
> OWASP notes. Import it fresh from `applied_jobs.csv` rather than reopening an
> Excel-saved copy.

## Comparing pay across currencies

Pay is normalised to an annual figure first, so `2-2.5 Lacs PA`, `₹25K–₹40K a month`
and `$30 an hour` all compare properly - and so do the formats Indeed's other country
sites use: `45.000 € pro Jahr`, `45 000 € par an`, `CHF 110'000`, `¥5,000,000`, and a
bare `$` on its Canadian, Australian, Singaporean, New Zealand or Mexican site, read
as that country's dollar. A salary is its first figure or range; the rest of the text
("plus 401k") is not pay. `--min-salary` needs a `--currency`, and most
postings are priced in another one; `--salary-basis` decides how they are compared:

| basis | ₹20,00,000 against a USD floor | when to use it |
|---|---|---|
| `ppp` (default) | ~$99,600 - what it is worth where it is earned | "which of these is the better job" |
| `market` | ~$21,000 - today's exchange rate | "what is this worth in my currency" |
| `strict` | not compared, flagged `salary-currency-mismatch` | when you would rather judge it yourself |

Purchasing power parity is the default because a market conversion makes every
Indian salary look small next to an American one, which is not a useful way to
choose a job. Exchange rates come from the ECB via frankfurter.dev and are cached in
`.money_cache.json`; every rate a run needs is fetched once, before any job is
compared.

PPP factors come from the World Bank indicator `PA.NUS.PPP`. The package ships a
read-only table, `src/applicant/ppp_factors.json`, so a fresh clone starts with
whatever is already known; factors you refresh go to `ppp_factors.json` in your
data directory and are layered over it. (They used to be written into the package
itself, which fails - or worse, succeeds - once applicant is installed.)

```
uv run applicant rates                          # what is cached right now
uv run applicant rates --refresh                # fetch everything missing
uv run applicant rates --refresh -c GBP SEK NZD # just these
uv run applicant rates --refresh --force        # re-fetch what is already there
uv run applicant rates --refresh --into src/applicant/ppp_factors.json   # maintainers
```

**The shipped table has only USD, INR and JPY.** The World Bank API throttles
hard - roughly one country per attempt - so the rest are fetched on demand and
cached as you use them, or all at once with `--refresh`. To give every fresh clone
more, refresh `--into` the shipped table and commit it.

Nothing is ever written from memory. A factor that cannot be retrieved is
reported and left out, and a comparison needing it falls back to a market rate
flagged `ppp-unavailable` rather than being handed an invented number.

Two things worth knowing before reading a converted figure: `EUR` maps to the
euro area aggregate, which is coarser than a single country, and `TWD` has no
factor at all because Taiwan is not a World Bank member.

## Checking where things stand

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

## Logging

Printed output is each command's answer - `status`' tally, `rates`' table, where a
file was written - and goes to **stdout**. Everything said about the work in
progress - which board answered, how many survived, which one refused - goes
through Python's `logging` to **stderr**, so the two never mix in a pipe. Every
subcommand takes:

| Flag | Console (stderr) shows |
|---|---|
| `-q` | warnings and failures only |
| *(default)* | progress too: what each source fetched, kept, saved |
| `-v` | debug detail, with a UTC timestamp and the module that spoke |
| `-vv` | all of that plus each HTTP request httpx makes |
| `--log-file PATH` | also write everything, at debug level, to `PATH` |
| `--no-log-file` | do not write a log file for this run |

```
uv run applicant search "python developer" -l India -q
uv run applicant search "python developer" -l India -v
uv run applicant financials zomato --log-file financials.log
```

`-q` silences commentary, never the answer: a quiet run still prints what it found.

**Every run records itself.** Unless you pass `--no-log-file`, it writes
`logs/run_20260812-143502.log`, named for when the run started (UTC), so they sort
chronologically and two never collide. The directory is created on the way and is
gitignored, and a run that logs nothing leaves no file. The file gets everything
down to debug whatever the console is showing, which is the point: a scrape you
left running is exactly the one whose output you no longer have. Nothing prunes
them - `rm -rf logs/` when you have had enough.

What is logged and how it is protected (the log inventory [OWASP ASVS 5.0](https://github.com/OWASP/ASVS/blob/v5.0.0/5.0/en/0x25-V16-Security-Logging-and-Error-Handling.md) 16.1.1 asks for):

- **Where:** stderr, and the log file. Nothing is sent anywhere else.
- **Format:** on the console at the default level, just the message. In the file
  and at `-v`: `2026-09-27T08:42:42Z WARNING applicant.search: naukri: ...` - UTC
  time, level, module, message.
- **Never logged:** passwords, cookies and session files. API keys given by flag or
  environment are masked as `***` anywhere they would appear, tracebacks included.
- **Log injection:** job titles and company names come from websites, so control
  characters are escaped on the console and in the file alike - a newline in a
  scraped title cannot forge a second entry.
- **The log file** is created readable by its owner only (`0600`).
- **Unexpected errors** print one line naming the error; the traceback goes to the
  log file (and to the console at `-v`). Ctrl-C exits with status 130.

Importing `applicant` configures no logging at all, so embedding it in another
program is silent until that program calls `applicant.log.configure()` or handles
the `applicant` logger itself.

## Usage
`uv run applicant -h` lists everything. `python -m applicant` works identically, and is
what to use without `uv`.

1. `uv run applicant --help` for the commands; `uv run applicant <command> --help` for each
2. `uv run applicant jobs` signs in to LinkedIn and scrapes your recommended jobs:
   `cookies.json` holds the session and `job_listing.json` the jobs
3. `uv run applicant --version`

A command is required. (Before 0.2.0, `applicant` alone or with bare flags meant
`applicant jobs`; it is now a usage error.) The logging and data flags - `-v`, `-q`,
`--log-file`, `--no-log-file`, `--data-dir`, `--store`, `--config` - go before or after
the command: `applicant -v search x` and `applicant search x -v` are the same.

### Exit codes

What ended a run, for a script deciding whether to retry:

| Code | Meaning |
|---|---|
| 0 | done |
| 1 | nothing to do, nothing matched, or an unexpected error (one line on stderr) |
| 2 | a usage or settings error: bad flags, a bad `applicant.toml` |
| 3 | blocked: a bot check, a login wall, a rate limit - retry later |
| 4 | not found: the company, slug or url resolves to nothing |
| 5 | unparseable: the page arrived without the data - the site changed |
| 6 | an API key refused, or out of credits |
| 7 | unreachable: the network failed through every retry - retry later |
| 130 | interrupted (Ctrl-C) |

## Where your data lives

Everything goes in one **data directory** - the current directory unless you say
otherwise - and every file flag (`-o`, `-i`, `--log`, ...) is relative to it:

```
uv run applicant search "python developer" --data-dir ~/applicant
export APPLICANT_HOME=~/applicant        # the same, for every run
```

Settings come from, in order - later wins: the defaults, an `applicant.toml` in
the current directory (or `--config PATH`), the environment, and flags.

```toml
# applicant.toml
data_dir = "~/applicant"
store = "sqlite"                 # or "files"

[files]
listing = "job_listing.json"     # any file name can be changed here
```

A relative `data_dir` in `applicant.toml` is relative to that file, so
`data_dir = "data"` means the `data` folder beside it wherever you run from. On
the command line (`--data-dir`, `APPLICANT_HOME`) it is relative to the current
directory, as paths there usually are.

### Saved searches

A search worth running is usually worth running again. Save it in `applicant.toml`
under a name, using the long flag names (`--posted-within` is `posted_within`):

```toml
[searches.ai-ml]
keywords = "ai ml engineer"
location = "India"
source = ["naukri", "linkedin"]
experience = 3
title = ["ai", "ml", "machine learning", "data scientist"]
posted_within = 14
```

```
uv run applicant search --saved ai-ml                 # the twelve flags, by name
uv run applicant search --saved ai-ml -t "nlp"        # a flag given here wins
uv run applicant search --list-saved
```

A misspelt field, or a board that doesn't exist, is reported when the file is read,
not ignored.

API keys are read from the environment (`CRUNCHBASE_API_KEY`, `TRACXN_API_KEY`) or
their flags only. A config file that sets one is refused, because config files get
committed.

**`applicant.db` is the record; the JSON and CSV are its exports.** With the
default `--store sqlite`, the jobs, applications and ratings live in an SQLite file
beside them. `job_listing.json` and `applied_jobs.csv` are rewritten after every
change, byte for byte as before, so a Google Sheets import works exactly as it did.

- **An existing directory is picked up on first use.** Point a new version at the
  files from an old one and they are imported. Nothing is converted or deleted.
- **The file you see is the data the next run uses.** Edit `job_listing.json` by
  hand, replace it, or delete it, and the next run imports what is there. The
  database notices that the file no longer matches what it last wrote.
- **Ratings are kept over time.** `company_reviews.json` holds this run's ratings,
  as always. Every rating ever read stays in the database.
- A command that only looks, in a directory with nothing in it, creates nothing.

`--store files` (or `store = "files"`) keeps the JSON and CSV alone, with no
database. Used as a library, applicant defaults to that, so importing it never
creates a database behind anyone's back.

## Where things are

```
src/applicant/
  domain/        the pure core: no network, no files, and a test that keeps it so
    job.py         the Job model, and required experience read out of text
    filtering.py   JobFilter, compiled to one Check per criterion
    capability.py  Field, and what a board filters on and publishes
    flags.py       every flag a posting can carry - a file format, so fixed
    rates.py       currency conversion from a RateSnapshot handed in
    dedupe.py      when postings on different boards are one job
    dates.py salary.py places.py   posting dates, pay, and where a place is
  errors.py      one error hierarchy for every source: Blocked, NotFound, Unreachable, ...
  infra/         the shared HTTP client (retries, backoff, 429s, per-host pacing),
                 the one browser launcher (Chrome, then Edge, then bundled), and
                 store/: applicant.db and the file exports kept in step with it
  filters.py     JobFilter wired to live rates and the board table; prepared()
  money.py       the FX and PPP cache, and Rates.snapshot() for a run
  ppp_factors.json  the shipped, read-only PPP table; refreshes go to the data dir
  settings.py    Settings: data dir, file names, store, keys - flags > env > applicant.toml
  boards/        one module per job board, all returning Job, and their capabilities;
                 linkedin_apply.py is the only code that acts on an account
  reviews/       company ratings from AmbitionBox and Glassdoor
  financials/    company funding from Crunchbase and Tracxn, tracked over time
  storage.py     job_listing.json and applied_jobs.csv
  files.py       atomic JSON writes; unreadable stores are moved aside, never overwritten
  services/      what each command does, callable without the command line:
                 search, apply, reviews, financials, rates, status; fan_out
                 isolates each source's failure, and events report progress
  search.py      Jobs: the thin front door over the search and apply services
  interaction.py how a --login run asks the person at the keyboard
  log.py         logging: stderr, levels, per-run file, escaping, secret masking
  cli/           one module per subcommand, each calling a service; render.py
                 is the one place results are printed
  __main__.py    python -m applicant
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

## Company financials
`applicant financials` looks up a company's funding from Crunchbase and Tracxn - total
raised, each round with its amount, lead investors and post-money valuation, latest
valuation, revenue, headcount and stage - and **tracks it over time** in
`company_financials.json`. Each run is compared with the last one and what moved is printed:

```
uv run applicant financials zomato swiggy --rounds
uv run applicant financials --from-jobs job_listing.json -s crunchbase
```

```
crunchbase: Zomato - raised $2.4B over 21 rounds, last Series K on 2026-09-01, revenue $1B to $10B
  changed: total funding: $2.1B -> $2.4B
  changed: new round: Series K on 2026-09-01 ($300M; led by Temasek)
```

- `--from-jobs` tracks every company in your scraped jobs, so you can see who is freshly
  funded (or has not raised in years) before applying. `Pvt. Ltd.`, `Inc.` and similar are
  ignored when matching names.
- `-s/--source` picks `crunchbase`, `tracxn` or `both` (default). A source that fails is
  reported and skipped rather than losing the other one's results.
- The history only gains a snapshot when something changed, so it reads as a change log. A
  figure a later run could not read (a blurred page, a field outside your plan) keeps its
  last known value instead of being reported as gone.
- Nothing is guessed: `Undisclosed` rounds have no amount rather than zero, and anything a
  source would not give is listed under `notes`.

Each source works two ways:

| | With an API key | Without one |
|---|---|---|
| **Crunchbase** | `CRUNCHBASE_API_KEY` or `--crunchbase-key`: the v4 entity lookup; names are resolved with its autocomplete | reads the organization page in a browser profile (`.cb_profile/`); takes a name, permalink or `crunchbase.com/organization/...` url |
| **Tracxn** | `TRACXN_API_KEY` or `--tracxn-key`: resolves a name, domain or entity id, then pulls the company, its funding rounds and the yearly valuation and revenue series | reads a public profile in a browser profile (`.tx_profile/`); **needs the profile url** (`tracxn.com/d/companies/<name>/__<id>`), since Tracxn search is behind a login |

- Neither source publishes an official client library, so both are called directly over
  `httpx`, following Crunchbase's v4 spec and Tracxn's Postman collection.
- **Crunchbase's free Basic API key does not include funding data.** The run says so
  rather than returning an empty record; without a paid key, leave it unset and the website
  is read instead.
- **Tracxn bills a credit per entity returned**, so rounds are fetched only up to
  `-n/--max-rounds` (default 20). An out-of-credits response stops immediately - retrying
  cannot help. Tracxn does not publish its response fields, so records are matched on field
  names rather than fixed paths.
- Both sites are behind bot checks. As with Glassdoor, the first browser run needs
  `--login`: a visible browser opens, you clear the check (and sign in, for more detail)
  once, and the profile is reused headlessly afterwards.

The whole thing is importable, with `Jobs` as the front door:

```python
from applicant.search import Jobs
from applicant.filters import JobFilter

with Jobs() as board:
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

Nothing is printed. Progress is logged under the `applicant` logger, and anything
you may want to show as it happens is emitted as an event - pass `emit=` to see
them:

```python
from applicant.services.events import BoardSearched, SourceFailed


def show(event):
    if isinstance(event, BoardSearched):
        print(f'{event.source}: {event.kept} of {event.seen}')
    elif isinstance(event, SourceFailed):
        print(f'{event.source} failed: {event.error}')


hits = Jobs().search('python developer', emit=show)
```

Every command has a service behind it in `applicant.services` - `fetch_reviews`,
`track_financials`, `summarise`, and so on - so a program can do anything the
command line does. A source that needs a person (a `--login` run clearing a bot
check, LinkedIn's one-time code) asks through `interaction=`; the default asks on
the terminal, and `applicant.interaction.Scripted` answers from a list.

`Job` is a Pydantic model, so postings are validated as they are built - blank
strings become `None`, whitespace is stripped, and a year range is derived from
whatever the board said (`experience_text`) into `experience_min` / `experience_max`.

Individual boards work standalone too, and all four return the same `Job` objects:

```python
from applicant.boards.indeed import Indeed

for job in Indeed().search('python developer', 'remote', limit=10):
    print(job.title, job.company, job.location, job.salary)
```

LinkedIn comes in two parts, because reading job cards and acting on your account
are different risks. `LinkedInGuest` (`applicant.boards.linkedin`) is the logged-out
search, and all a search ever loads. `LinkedIn` (`applicant.boards.linkedin_apply`) adds
the signed-in flows: `login()`, `restore_session()`, `scrape_jobs()` for recommended
jobs, `easy_apply()`, and `apply(jobs, dry_run=...)`. That last one is the `Applier`
interface the apply service calls, and `dry_run` has no default. Sessions are stored
as Playwright storage state. A `cookies.json` from the old Selenium version is no longer
converted; `applicant jobs --overwrite` signs in again and replaces it.

`Jobs.apply()` requires `dry_run` too, keyword-only, and takes `confirm=`: a function
shown the postings before anything is sent.

Company ratings are also importable:

```python
from applicant.reviews import AmbitionBoxClient

rating = AmbitionBoxClient().fetch('tcs', max_reviews=40)
print(rating.overall_rating, rating.review_count)
print(rating.reviews[0].pros, rating.reviews[0].cons)
```

Both clients share the same `fetch(company, max_reviews)` signature and return a
`CompanyRating`, so callers never branch on the source.

Financials follow the same pattern:

```python
from applicant.financials import CrunchbaseClient, FinancialsTracker

financials = CrunchbaseClient().fetch('zomato')
print(financials.total_funding, financials.valuation, financials.revenue_range)
for funding_round in financials.rounds:
    print(funding_round.date, funding_round.round, funding_round.amount)

changes = FinancialsTracker('company_financials.json').record(financials)
```

`CrunchbaseClient` and `TracxnClient` both return a `CompanyFinancials` (a Pydantic model);
amounts are `Money` values carrying the currency, the USD figure where the source gave one,
and the text as it was written.
