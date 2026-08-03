"""Scrapes applicable job postings, applies to them, and looks up company ratings.

    from applicant.search import Jobs
    from applicant.filters import JobFilter

    hits = Jobs().search('python developer', JobFilter(location='India'))

The layout, roughly in dependency order:

* `models`    - the `Job` dataclass and the error hierarchy every board shares
* `dates`     - relative and epoch posting dates into ISO ones
* `salary`    - reading pay off a posting, normalised to an annual figure
* `browser`   - launching Playwright in a way the boards will accept
* `filters`   - `JobFilter`, and the flags explaining what could not be checked
* `storage`   - the job listing file and the application log
* `boards/`   - one module per job board, all returning `Job`
* `reviews/`  - company ratings from AmbitionBox and Glassdoor
* `search`    - the facade tying the boards, filters and storage together
* `cli`       - argument parsing and the subcommand handlers
"""

from __future__ import annotations

__version__ = '0.1.0'

__all__ = ['__version__']
