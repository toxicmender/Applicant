"""Keeps a history of each company's financials and says what changed.

    from applicant.financials import CrunchbaseClient, FinancialsTracker

    tracker = FinancialsTracker('company_financials.json')
    changes = tracker.record(CrunchbaseClient().fetch('zomato'))
    # ['total funding: $2.1B -> $2.35B', 'new round: Series K on 2026-09-01 ($250M)']

The file holds, per source and company, the latest known figures and a list of
snapshots. A snapshot is only appended when something actually changed, so the
history reads as a change log rather than a copy per run.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from pydantic import ValidationError

from ..files import read_document, write_document
from .models import CompanyFinancials, FundingRound, Money

logger = logging.getLogger(__name__)

# field -> label, in the order changes are reported
TRACKED = [
    ('total_funding', 'total funding'),
    ('funding_rounds_count', 'funding rounds'),
    ('last_funding_type', 'last funding type'),
    ('last_funding_date', 'last funding date'),
    ('valuation', 'valuation'),
    ('revenue', 'revenue'),
    ('revenue_range', 'revenue range'),
    ('employees', 'employees'),
    ('stage', 'stage'),
    ('ipo_status', 'IPO status'),
]


def company_key(financials):
    ident = financials.company_id or re.sub(r'[^a-z0-9]+', '-', financials.company.lower()).strip(
        '-'
    )
    return '{}:{}'.format(financials.source, ident.lower())


class FinancialsTracker:
    """The change log of each company's financials, in a file or in applicant.db.

    `backend='files'` keeps company_financials.json alone. `backend='sqlite'`
    keeps the history in applicant.db beside it and exports the same file, byte
    for byte; a file edited or replaced by hand is imported and wins. The change
    rules below are the same either way - only loading and saving differ.
    """

    def __init__(self, path='company_financials.json', backend: str = 'files'):
        self.path = path
        self.backend = backend
        self.data = self._load()

    def _load(self):
        # the tracker exists to write: an unreadable history is moved aside
        # rather than replaced, since it is the only copy of every snapshot
        if self.backend == 'sqlite':
            from ..infra.store.sqlite import Store

            with Store.beside(self.path) as store:
                data = store.financials_document(self.path)
        else:
            data = read_document(self.path, quarantine=True)
        if not isinstance(data.get('companies'), dict):
            data['companies'] = {}
        return data

    def save(self):
        if self.backend == 'sqlite':
            from ..infra.store.sqlite import Store

            with Store.beside(self.path) as store:
                store.save_financials(self.data, self.path)
        else:
            write_document(self.path, self.data)

    def latest(self, key):
        entry = self.data['companies'].get(key)
        return self._previous(key, entry) if entry else None

    def _previous(self, key, entry):
        """The stored latest record, or None if it no longer validates."""
        try:
            return CompanyFinancials(**entry['latest'])
        except (KeyError, TypeError, ValidationError) as error:
            logger.warning(
                f'{self.path}: stored record for {key} is invalid ({type(error).__name__}); '
                'comparing against nothing this time, earlier snapshots are kept'
            )
            return None

    def history(self, key):
        return list((self.data['companies'].get(key) or {}).get('snapshots', []))

    def record(self, financials, save=True):
        """Store a fetch and return what changed since the last one, as readable lines.

        The first fetch of a company returns an empty list. A field the new fetch
        could not read keeps its previous value instead of being reported as a
        change - a logged out page hiding a figure is not the figure disappearing.
        """
        key = company_key(financials)
        entry = self.data['companies'].get(key)
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

        previous = self._previous(key, entry) if entry else None
        if previous is None:
            merged, changes = financials, []
            first_seen = True
        else:
            merged, changes = merge(previous, financials)
            first_seen = False

        snapshots = entry.get('snapshots') if entry else None
        if not isinstance(snapshots, list):
            snapshots = []
        if first_seen or changes:
            snapshots.append({'recorded_at': now, 'changes': changes, 'data': merged.to_dict()})

        self.data['companies'][key] = {
            'source': financials.source,
            'company': merged.company,
            'first_seen': (entry or {}).get('first_seen') or now,
            'last_checked': now,
            'latest': merged.to_dict(),
            'snapshots': snapshots,
        }
        if save:
            self.save()
        logger.debug(
            f'{key}: '
            + (
                'first snapshot'
                if first_seen
                else f'{len(changes)} change(s)'
                if changes
                else 'unchanged, no snapshot'
            )
        )
        return changes


def merge(previous, current):
    """-> (the combined record, the list of changes from previous to current)."""
    merged = current.model_copy(deep=True)
    changes = []

    for field, label in TRACKED:
        old, new = getattr(previous, field), getattr(current, field)
        if new is None:
            setattr(merged, field, old)
            continue
        if old is None or _same(old, new):
            continue
        changes.append('{}: {} -> {}'.format(label, _show(old), _show(new)))

    known = {item.key() for item in previous.rounds}
    for item in current.rounds:
        if item.key() not in known:
            changes.append('new round: {}'.format(describe_round(item)))

    # keep rounds the new fetch did not show (a shorter max_rounds, a blurred page)
    kept = {item.key(): item for item in previous.rounds}
    kept.update({item.key(): item for item in current.rounds})
    merged.rounds = sorted(kept.values(), key=lambda r: r.date or '', reverse=True)

    for field in (
        'description',
        'website',
        'founded',
        'stock_symbol',
        'url',
        'valuation_date',
        'revenue_year',
    ):
        if getattr(merged, field) is None:
            setattr(merged, field, getattr(previous, field))
    return merged, changes


def describe_round(item: FundingRound):
    text = item.round or 'round'
    if item.date:
        text += ' on {}'.format(item.date)
    details = []
    if item.amount is not None:
        details.append(str(item.amount))
    if item.post_money_valuation is not None:
        details.append('valued at {}'.format(item.post_money_valuation))
    if item.lead_investors:
        details.append('led by {}'.format(', '.join(item.lead_investors)))
    return text + (' ({})'.format('; '.join(details)) if details else '')


def _same(old, new):
    if isinstance(old, Money) and isinstance(new, Money):
        return old.same_as(new)
    return old == new


def _show(value):
    return str(value) if value is not None else 'unknown'
