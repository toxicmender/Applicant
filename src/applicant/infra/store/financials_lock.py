"""One owner per financials export, shared by the file and SQLite backends."""

from pathlib import Path

from ...errors import FinancialsBusy
from ..advisory_lock import advisory_lock


def financials_lock(file):
    target = Path(file).resolve()
    return advisory_lock(
        target.with_name(f'.{target.name}.lock'),
        lambda: FinancialsBusy(f'{target}: financials history is in use; rerun the command'),
    )
