"""Scrapes applicable job postings, applies to them, and looks up company ratings
and financials.

    from applicant.search import Jobs
    from applicant.filters import JobFilter

    hits = Jobs().search('python developer', JobFilter(location='India'))

The layout, roughly in dependency order:

* `domain/`   - the pure core: `Job`, filtering, capabilities, flags, rates, dedupe
* `errors`    - one error hierarchy for every source
* `infra`     - the shared HTTP client and the one browser launcher
* `filters`   - `JobFilter` wired to live rates and the board table
* `money`     - the FX and PPP cache behind cross-currency comparisons
* `storage`   - the job listing file and the application log
* `boards/`   - one module per job board, all returning `Job`
* `reviews/`  - company ratings from AmbitionBox and Glassdoor
* `financials/` - company funding from Crunchbase and Tracxn, tracked over time
* `search`    - the facade tying the boards, filters and storage together
* `cli`       - argument parsing and the subcommand handlers
* `log`       - logging setup: stderr, escaping, secret masking, UTC timestamps
"""

from __future__ import annotations

import logging

# silent unless the caller configures logging; the CLI does so in applicant.log
logging.getLogger(__name__).addHandler(logging.NullHandler())

__version__ = '0.1.0'

__all__ = ['__version__']
