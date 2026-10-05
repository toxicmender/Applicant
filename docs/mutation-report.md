# Mutation report

The last mutation pass over `src/applicant/domain` and `src/applicant/services`,
run with mutmut 3.8 as [maintainers.md](maintainers.md#a-mutation-pass) describes.
A mutant is one small change to the code: an operator flipped, a constant
changed, an argument dropped. A test that fails because of it kills it; a mutant
every test passes with survives, and is either a gap in the tests or a change
that cannot alter behaviour.

| | Mutants | Killed | Survived | No tests |
|---|---|---|---|---|
| First run | 1634 | 1317 | 315 | 2 |
| After `tests/test_hardening.py` | 1634 | 1426 | 206 | 2 |
| This pass (3 removed with dead code) | 1631 | 1453 | 176 | 2 |

Every one of the 178 left is accounted for below. None is a behaviour the
program has and the tests do not check.

## What the pass found and fixed

Killed by tests in `tests/test_hardening.py` (the class is named for each):

- **Salary** (`SalaryEdgesTest`): the 1900-2100 "this is a year" band at both
  ends; a second figure not joined as a range; a unit making a dotted figure a
  decimal; which separator is the decimal; NBSP, narrow NBSP and both
  apostrophes as digit groupers; each end of a range keeping its own unit
  (`80k - 1.2L`).
- **Experience** (`ExperienceEdgesTest`): a minimum beyond a working life
  (`99+ years`) is not believed.
- **Filtering** (`FilterEdgesTest`): pay exactly at the minimum; a converted
  salary's flag; a pure filter using its own rates; one experience bound ruling
  a job out; a timestamp read by its date; a currency counting only with a
  minimum; a posting with no company never matching a company filter.
- **Dedupe** (`DedupeEdgesTest`): every part of the key and fingerprint; copies
  with no fingerprint never merged; equal reach keeping the first; Easy Apply
  beating an earlier plain url; three copies keeping their own flags.
- **Places and dates** (`PlacesAndDatesTest`): the longer place name winning,
  also when one name is a prefix of the other; an epoch read in UTC on a date
  where the machine's zone would change it; "today" being the UTC date.
- **Apply** (`ApplyServiceEdgesTest`): a client keeping no record; a client's
  failed jobs; a crashing client; a dry run; an interrupt carrying what was
  sent and what failed; an interrupt saving the other boards' rows; the count
  of already-settled jobs; a salary floor fetching rates once.
- **Search** (`SearchServiceEdgesTest`): rounds growing until enough survive,
  stopping as soon as enough do, and the last round not asking for more; a
  board filtering dates itself; the rates a page needs; enrichment going on
  past a page that says nothing, reusing pages already read once the budget is
  spent or a site refuses, and the held-back experience check keeping the
  filter's `keep_unknown`.
- **Fan-out, financials, rates, reviews, status**: each outcome and event
  naming its source; companies tracked once whatever their case, and a posting
  with no company adding none; the round limit reaching the client; a factor
  without a year; refresh passing its choices on; a failing review site
  reported through `emit`; the `status --json` shape.

Bugs: none in this pass. The four the fuzz tests found (`tests/test_fuzz.py`)
are fixed and listed in the CHANGELOG.

Dead code removed:

- `companies_to_track` kept its own record of companies it had added from the
  listing, but `companies_from_jobs` already gives each company once.
- `experience_from` returned None when the phrase it matched held no number,
  but the phrase is what a pattern requiring a number matched.

## The 178 left, and why each cannot change behaviour

**Log text (67).** The wording, arguments or `exc_info` of a log line. Logs are read by
people, and pinning their wording would make every rewording a test failure.
Two log lines that are behaviour are tested: the "already settled" count and the
"enrichment stopped" warning.

**Default parameter values (29, including the 2 with no tests).** A default
every caller overrides: `backend='files'`, `log='applied_jobs.csv'`,
`limit=25`, `max_rounds`, `pause`, `max_reviews`, `enrich_limit`, `force`,
`basis='ppp'`. The CLI passes each one, and the CLI tests check what it passes.
The two with no tests are `Board.search`'s defaults: `Board` is a `Protocol`,
whose method body never runs.

**A dropped job's verdict (21).** `Verdict(False, ...)` becoming
`Verdict(None, ...)`, or losing its reason or flag. `None` and `False` are both
"drop"; the reason feeds only a debug line, and a dropped job's flags are
thrown away with it.

**The store backend passed on (15).** `listing(path, backend)` becoming
`listing(path, None)`, and the same for `applications`, `companies_from_jobs`,
`FinancialsTracker` and `ApplyToJobs.backend`. With `sqlite` the JSON and CSV
files are still written as exports, and the database imports a file it sits
beside on every read, so reading through either backend gives the same rows.
The backend is checked where it does differ: the history the database keeps
(`test_store.py`).

**The rest (46), by module:**

- `domain/salary` (13):
  - `'\u00a0'` written `'\u00A0'`, and `'\u202f'` written `'\u202F'`: the same
    string.
  - `>` → `>=` when picking the decimal: the two positions are never equal once
    both separators are present. `rpartition` → `partition`: it only runs with
    one comma, or its result is thrown away.
  - `rfind` → `find` for either separator: different only when the separators
    alternate (`1.234,567.89`), which no locale writes. Equivalent on real
    figures; a deliberately odd one would be read differently.
  - the four dash replacements: `RANGE` accepts `–` and `—` itself, and allows
    the few letters the mutant pads around `-`.
  - `multiplier > 1` → `> 2` (twice): multipliers are 1 or at least 1000.
  - `_multiplier(suffix or 'XXXX')`: an unknown suffix is 1 either way.
- `domain/dedupe` (7): the `_flatten` and `fingerprint` separators, and a
  missing location's placeholder, change every fingerprint alike, so equal
  stays equal; `reach` returning 3 instead of 2 keeps the order (Easy Apply is
  still highest); `merge_flags` stores its flags as dict keys, so the value is
  unused; `best.get(mark or 'XXXX')` and `and` → `or` only differ for a job with
  no fingerprint, which takes the branch before.
- `domain/filtering` (2): `top is None` after `salary is None` (`or` → `and`)
  cannot matter, because a parsed salary always has a top (a fuzz property
  checks it); `flag = ""` instead of `None`, since an empty flag is never
  added.
- `domain/places` (3): `_pattern` sorted without `key=len`, in reverse
  alphabetical order, still puts a longer name before any name it starts with;
  `countries_in(None)` with a placeholder matches no country.
- `domain/dates` (1): `.upper()` instead of `.lower()` on the count: only digits
  and "a"/"few" reach it, and `isdigit` is the same for both cases.
- `domain/rates` (1): `ppp(None)` looking up a placeholder: no such currency.
- `services/search` (9): the board name passed to `_from_board` and
  `close_quietly`, which use it only in log lines; `seen` and `kept` start values, overwritten by the
  first round, which always runs; the round count when `want` is None, where
  the loop breaks after one round anyway; `kept[:want]` when `want` is None,
  which is all of `kept`; `_recheck_experience`'s `keep_unpublished`, which only
  matters for a board that says it does not publish a field, and the recheck
  is not given one.
- `services/fanout` (5): the four `about` mutants are log text; `expected=True`
  dropped is `SourceFailed`'s default.
- `services/apply`, `services/financials` (5): `worklist`'s and
  `track_financials`' `if not`, which only choose whether to log; the source
  name `_apply_isolated` uses only in its log lines; and the two mutants of
  `companies_to_track`'s `backend` default.
