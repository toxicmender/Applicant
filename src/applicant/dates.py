"""Kept so `from applicant.dates import ...` goes on working; see `applicant.domain.dates`."""

from .domain.dates import RELATIVE, UNITS, epoch_to_iso, relative_to_iso

__all__ = ['RELATIVE', 'UNITS', 'epoch_to_iso', 'relative_to_iso']
