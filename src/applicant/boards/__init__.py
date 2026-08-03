"""One module per job board, each returning `applicant.models.Job`.

Clients are imported lazily by `applicant.search.Jobs` so that a board needing a
browser costs nothing until it is actually used.
"""

from __future__ import annotations
