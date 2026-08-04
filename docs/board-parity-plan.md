# Filtering across the job boards

Written against `claude/job-filter-testing-08074p` (`28cc6d2`), driven by what
`tests/test_target_job_filters.py` found: a shortlist of eight AI/ML postings in
Indian metros is reachable, but only by running the tool four times and knowing
which flags quietly do nothing on which board.

The four boards are not four ways of doing the same thing. They differ in what
they will filter for us, and — more awkwardly — in what they will *tell* us.
`JobFilter` is written as if they were uniform, and the gap between those two
facts is where results are lost.

**Status: proposed. Nothing here is built.**

---

## 1. What each board actually gives us

Read off the four board modules, not off the README.

| | LinkedIn | Indeed | Naukri | Google Jobs |
|---|---|---|---|---|
| How it is fetched | guest endpoint, no browser | JSON blob, browser on challenge | browser, API intercepted | browser, Search results read structurally |
| Filters **location** itself | yes, `location=` | yes, `l=` **and the host** | yes, in the url slug | yes, `"… in <location>"` |
| Filters **date** itself | yes, `f_TPR=r<secs>` | yes, `fromage=<days>` | no | no |
| Publishes **experience** | no | no | **yes** | no |
| Publishes **salary** | no | yes | yes | sometimes¹ |
| Publishes **posting date** | yes, ISO | yes, epoch | yes, relative | sometimes¹ |
| Publishes a **url** | yes | yes | yes | **no**² |
| Publishes an **id** | yes | yes | yes | no, synthesised³ |

1. `googlejobs.py:116-159` classifies a card's visible lines by regex. A salary
   line is recognised when it carries a currency symbol or a period word; a date
   line when it says "ago"/"today". Both are best-effort, not fields.
2. `googlejobs.py:158` — Google gives no posting url; applications route back to
   the originating board, reported as `via`.
3. `googlejobs.py:144-147` — a SHA1 of title/company/location/via, so cards
   dedupe against each other rather than collapsing into one.

Two rows of that table carry the whole problem. **Experience is published by one
board in four, and the url by three in four.** Everything below follows.

---

## 2. What that costs today

Each of these is pinned by a test in `tests/test_target_job_filters.py`, so the
plan below has a way to prove it changed something.

### 2.1 `-e` is real on Naukri and decorative elsewhere

The shortlist is defined by experience: one to five years. On Naukri that filter
does exactly what it says. On the other three boards every posting comes back
`experience-unknown` and is kept regardless, so `-e 3` neither includes nor
excludes anything — the user believes they filtered and did not.

`--strict` then makes it worse rather than better: it drops everything that could
not be checked, which is *all* LinkedIn, Indeed and Google results. A flag whose
documented job is "be more careful" silently deletes three of four sources.

The `keep_unknown` decision (`filters.py:93-99`) cannot distinguish "this posting
did not say" from "this board never says". It should.

### 2.2 `--min-salary` is real on two and a half

Same shape, less severe. LinkedIn guest cards carry no pay at all
(`linkedin.py:134-143`), Google's is a heuristic. On a shortlist like this one —
where no posting states pay — every result comes back `salary-unknown`, which is
honest but means the filter is doing nothing.

### 2.3 Location is a country at the board and a city in the file

`Jobs.search` hands location to the board and skips the local re-check
(`search.py:124`), because a country search legitimately answers with bare city
names. That is correct, and it is why `search -l India` works.

But the same string is checked *locally* by `apply` (`cli.py:133-139`), which has
no board to delegate to. `apply -l India` compares "India" against
"Bengaluru, Karnataka" and matches nothing at all: `0 of 12 stored jobs match`.
The user's filter did not narrow their worklist, it emptied it.

There is a second, quieter version of this on Indeed. `host_for`
(`indeed.py:54-60`) picks the country site by looking for a country name *in the
location string*, defaulting to the US. So `-l Bengaluru` — the string that works
locally and on Naukri — sends Indeed to `indeed.com` and returns American jobs.
The one spelling that satisfies the local filter is the one that breaks Indeed.

### 2.4 One `--title` cannot express one shortlist

`--title` is a single AND-of-words string (`filters.py:80-83`). These eight
postings span "AI", "ML", "Machine Learning" and "Data Scientist", and no single
value reaches more than six of them. Four runs into the same output file do work,
because writes merge — but the user has to know that.

### 2.5 `-n` is counted before filtering

`-n 25` pulls 25 per board and *then* filters, so a filter tight enough to be
useful returns a handful. The README says to raise the limit; nothing tells the
user how far, and the answer differs per board because the boards differ in how
much of the filter they applied themselves.

### 2.6 The same job from three boards is three rows

`_key` (`storage.py:44-53`) is `(source, id)`, and `ApplicationLog.existing_keys`
(`storage.py:95-102`) is the same pair. A posting that appears on LinkedIn, Indeed
and Google Jobs is three stored jobs and, at apply time, three rows in the
worklist — the CSV's never-twice guarantee is per board, not per job. For Google
Jobs specifically the row also has no url, so the worklist entry cannot be acted
on without searching again.

---

## 3. The design

One idea does most of the work: **boards should declare what they do and what
they know, and the filter should read that declaration.** Everything else is a
consequence.

### 3.1 Boards declare capabilities

Today `search.py:45` carries `NATIVE_DATE = ('linkedin', 'indeed')` — a fact
about two board modules, written down in a third, by name. Adding a board means
remembering to edit a tuple somewhere else.

```python
# boards/__init__.py
@dataclass(frozen=True)
class Capability:
    # what the board applies server side, so a local re-check would be wrong
    filters: frozenset[str] = frozenset()
    # what its cards actually carry, so silence can be attributed correctly
    publishes: frozenset[str] = frozenset()
```

| Board | `filters` | `publishes` |
|---|---|---|
| LinkedIn | location, posted | posted, url, id |
| Indeed | location, posted | salary, posted, url, id, employment_type, remote |
| Naukri | location | **experience**, salary, posted, url, id |
| Google Jobs | location | salary, posted, employment_type, via |

`Jobs.search` then derives its `skip` from `board.capability.filters` instead of
hardcoding it, and `NATIVE_DATE` goes away. This is a pure refactor with no
behaviour change, which makes it the safe thing to land first.

### 3.2 Flags say *who* could not tell us

`matches()` gains the board's `publishes` set and splits one flag into two:

| Flag | Means |
|---|---|
| `experience-unstated` | the board publishes experience; this posting did not state it |
| `experience-unpublished` | this board never publishes experience — nothing was checked, and nothing could have been |

Same for `salary-*`. The distinction is not cosmetic: it is the difference
between a posting being evasive and a source being silent, and only the first is
a reason to drop anything.

`--strict` gains an optional value, defaulting to today's meaning so existing
invocations are untouched:

```
--strict            # as now: drop anything unverifiable
--strict published  # drop only where the board could have told us and the posting did not
```

`--strict published -e 3` is then the flag a user actually wants: it tightens
Naukri without deleting the other three boards.

### 3.3 Location: containment, and a country for Indeed

A small place table — the project is already India-centric (Naukri, AmbitionBox,
a checked-in INR PPP factor) — mapping a country to its states and metros:

```python
CONTAINS = {'india': {'karnataka', 'bengaluru', 'bangalore', 'telangana',
                      'hyderabad', 'maharashtra', 'pune', 'mumbai', ...}}
```

Two uses, both of which pay for it:

- **Locally**, `_text_matches` on location becomes containment-aware, so
  `apply -l India` keeps the Bengaluru postings instead of emptying the worklist.
  Where the table cannot decide — a country we have no entry for, against a city
  we do not recognise — the posting is **kept and flagged `location-unverified`**,
  which is what the rest of this codebase does with things it cannot check, rather
  than silently dropping a correct result.
- **Outbound**, the country is appended when a board needs it, so `-l Bengaluru`
  reaches Indeed as `Bengaluru, India` and `host_for` resolves `in.indeed.com`
  instead of the US site.

### 3.4 `-t` and `-c` become repeatable

`action='append'`, matching **any** of the given values (each value keeping its
current all-words-in-any-order rule):

```
uv run applicant search "AI ML engineer" -l India -e 3 \
    -t ai -t ml -t "machine learning" -t "data scientist"
```

One run, all eight, no decoys. A single `-t` behaves exactly as it does today.

This also blunts the substring overreach the tests pin down (`-t ai` matching
"Graduate Trainee"), because the user no longer has to pick one short word to
cover several title families.

### 3.5 Ask for survivors, not for pulls

`-n` keeps its meaning. A new `--want N` pages each board until N postings have
*survived* the filter or `--max-pages` is reached, which is the number the user
actually has in mind. Per board, so a board that answers a tight filter well is
not held back by one that does not.

### 3.6 One job, three boards, one application

Add a cross-board fingerprint — normalised `(company, title, city)` — alongside
the existing key. `job_listing.json` keeps every source's row, because throwing
away a source's copy loses its url and its fields. `ApplicationLog` dedupes on
the fingerprint as well as `(source, id)`, and when several rows describe the
same job, applying picks the one that can actually be acted on: a LinkedIn Easy
Apply row over a plain url, a url over Google's `via` with no link at all.

### 3.7 Enrichment, opt-in and last

The only real fix for §2.1 on three boards is to open the posting and read it.
`--enrich` would fetch the detail page for postings that survived every other
filter, parse experience and salary out of it with the existing helpers, and
re-run the checks that were previously unverifiable.

Deliberately last, and deliberately opt-in: it is N more requests against
bot-guarded sites, on the slowest path, and it is the part most likely to break
when a site is redesigned. Bounded by a count, off by default, and a failure to
enrich must leave the posting flagged rather than dropped.

---

## 4. Sequencing

Six phases, each independently shippable, each with tests that do not touch the
network. The shortlist in `tests/test_target_job_filters.py` is the acceptance
test throughout: it should take fewer runs and fewer surprises at each phase.

| Phase | Change | Test |
|---|---|---|
| 0 | `Capability` per board; `search.py` derives `skip`; `NATIVE_DATE` removed | existing suite stays green unchanged — that is the point |
| 1 | `-unstated` / `-unpublished` flags; `--strict published` | a LinkedIn posting survives `--strict published -e 3`; a Naukri one that states nothing does not |
| 2 | Location containment, `location-unverified`, country appended outbound | `apply -l India` keeps the eight; `host_for('Bengaluru')` reaches `in.indeed.com` |
| 3 | Repeatable `-t` / `-c` | one invocation returns all eight and none of the decoys |
| 4 | Cross-board fingerprint at apply time | the same job from three boards writes one worklist row, the actionable one |
| 5 | `--want` / `--max-pages` | a stub board paged until N survivors, and stopping at the cap |
| 6 | `--enrich` | fixture detail pages; a fetch failure leaves the flag, not a drop |

**If only two land, make them 2 and 3.** They are what this shortlist needs, they
are small, and neither depends on the refactor. Phase 0 and 1 are what make the
other four honest — without the capability declaration, every later phase has to
re-guess which board knows what.

A README correction belongs with phase 1: the filter table documents `-e` and
`--min-salary` without saying that three of four boards publish neither, which is
the single most misleading thing a new user meets.

---

## 5. What this does not do

- **No new boards.** Parity across the four that exist, not a fifth.
- **No change to `applicant jobs`.** The signed-in LinkedIn scrape takes no filter
  flags today and does not gain any here.
- **No move off the flat files.** The status store is `docs/ci-plan.md` §3.5's
  subject and stays there; §3.6 above is a dedupe key, not a schema.
- **No scoring or ranking.** Filters keep answering yes or no. "Which of these
  eight is the best job" is a different feature, and the reviews clients are
  already the beginning of it.
- **Nothing that guesses.** The existing rule holds throughout: what cannot be
  checked is kept and flagged, never invented and never quietly dropped.
