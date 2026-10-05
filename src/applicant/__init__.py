"""Scrapes applicable job postings, applies to them, and looks up company ratings
and financials.

    from applicant.search import Jobs
    from applicant.filters import JobFilter

    hits = Jobs().search('python developer', JobFilter(location='India'))

The layout, roughly in dependency order:

* `domain/`   - the pure core: `Job`, filtering, capabilities, flags, rates,
  dedupe, salary and date parsing, places, and the ports the core depends on
* `errors`    - one error hierarchy for every source, and for store and settings
* `log`       - logging setup: stderr, escaping, secret masking, UTC timestamps
* `files`     - atomic JSON writes, and unreadable files kept rather than lost
* `settings`  - where data lives, the store, keys and saved searches, decided once
* `interaction` - the few moments a source asks the person at the keyboard
* `infra/`    - the shared HTTP client, the one browser launcher, and the store:
  `applicant.db` with the JSON and CSV as its exports (`infra/store/`)
* `money`     - the FX and PPP cache behind cross-currency comparisons
* `filters`   - `JobFilter` wired to live rates and the board table
* `storage`   - the job listing file and the application log, and their rules
* `boards/`   - one module per job board, all returning `Job`
* `reviews/`  - company ratings from AmbitionBox and Glassdoor
* `financials/` - company funding from Crunchbase and Tracxn, tracked over time
* `services/` - what each command does - search, apply, reviews, financials,
  rates, status - with no printing, reporting through events
* `search`    - the `Jobs` facade most library callers use
* `cli/`      - one module per command: flags in, a service called, the answer out
"""

from __future__ import annotations

import logging

# silent unless the caller configures logging; the CLI does so in applicant.log
logging.getLogger(__name__).addHandler(logging.NullHandler())

# the one place the version is written; pyproject.toml reads it from here
__version__ = '0.2.0'

__all__ = ['__version__']
