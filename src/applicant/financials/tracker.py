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
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import ValidationError

from ..errors import Unparseable
from ..files import read_document, write_document
from ..infra.store.financials_lock import financials_lock
from .identity import company_name
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
HEADLINES = ('total_funding', 'valuation', 'revenue', 'revenue_range')


def _checked_at(entry: dict):
    latest = entry.get('latest')
    return entry.get('last_checked', latest.get('fetched_at') if isinstance(latest, dict) else None)


def _utc_timestamp(raw) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        value = datetime.fromisoformat(
            raw.removesuffix('Z') + '+00:00' if raw.endswith('Z') else raw
        )
        return value.astimezone(timezone.utc) if value.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None


def has_financial_data(financials: CompanyFinancials) -> bool:
    return any(getattr(financials, field) is not None for field in HEADLINES) or any(
        round_.date for round_ in financials.rounds
    )


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

    def __init__(self, path: str | Path = 'company_financials.json', backend: str = 'files'):
        self.path = path
        self.backend = backend
        self.target = Path(path).resolve()
        self._buffer: list[tuple[CompanyFinancials, datetime, bool]] = []
        with self._storage() as store:
            self.data, _ = self._load(store)
        self._saved_data = deepcopy(self.data)

    @contextmanager
    def _storage(self):
        with financials_lock(self.target):
            if self.backend == 'sqlite':
                from ..infra.store.sqlite import Store

                Store.preflight_financials(self.target)
                with Store.beside(self.target) as store:
                    yield store
            else:
                yield None

    def _load(self, store):
        # the tracker exists to write: an unreadable history is moved aside
        # rather than replaced, since it is the only copy of every snapshot
        if store is not None:
            data, baseline = store._load_financials(self.target)
        else:
            data = read_document(self.target, quarantine=True)
            baseline = None
        if not isinstance(data.get('companies'), dict):
            data['companies'] = {}
        return data, baseline

    def save(self):
        with self._storage() as store:
            if self._buffer:
                self._flush(store)
                return
            if self.data == self._saved_data:
                self.data, _ = self._load(store)
                self._saved_data = deepcopy(self.data)
                return
            _, baseline = store._sync_financials(self.target) if store is not None else (None, None)
            self._save(store, baseline)

    def _save(self, store, baseline):
        if store is not None:
            name = store._financials_name(self.target)
            store._stage_financials(name, self.data, baseline)
        else:
            write_document(self.target, self.data)
        self._buffer.clear()
        self._saved_data = deepcopy(self.data)
        if store is not None:
            store._reconcile_financials(name, self.target)

    def latest(self, key):
        entry = self.data['companies'].get(key)
        return self._previous(key, entry) if entry else None

    def cached_tracxn(self, company: str, *, now: datetime | None = None):
        requested = company_name(company)
        if not requested:
            return None
        matches = [
            (key, entry)
            for key, entry in self.data['companies'].items()
            if key.startswith('tracxn:')
            and isinstance(entry, dict)
            and isinstance(entry.get('company'), str)
            and company_name(entry['company']) == requested
        ]
        if len(matches) != 1:
            return None
        key, entry = matches[0]
        raw = _checked_at(entry)
        if raw is None:
            return None
        checked = _utc_timestamp(raw)
        if checked is None:
            logger.warning('%s: invalid or timezone-naive Tracxn timestamp; refreshing', key)
            return None
        age = (now or datetime.now(timezone.utc)) - checked
        if age < timedelta():
            logger.warning('%s: future Tracxn timestamp; check the system clock', key)
            return None
        if age >= timedelta(days=30):
            return None
        result = self._previous(key, entry)
        if (
            result is None
            or result.source != 'tracxn'
            or company_key(result) != key
            or company_name(result.company) != requested
            or not has_financial_data(result)
        ):
            return None
        return result, checked.isoformat()

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

    def record(self, financials, save=True, *, require_complete=False):
        """Store a fetch and return what changed since the last one, as readable lines.

        The first fetch of a company returns an empty list. A field the new fetch
        could not read keeps its previous value instead of being reported as a
        change - a logged out page hiding a figure is not the figure disappearing.
        """
        checked = datetime.now(timezone.utc).replace(microsecond=0)
        if not save:
            result = self._record(financials, checked, require_complete=require_complete)
            self._buffer.append((financials.model_copy(deep=True), checked, require_complete))
            return result
        with self._storage() as store:
            return self._flush(store, (financials, checked, require_complete))

    def _flush(self, store, current=None):
        previous_data = self.data
        previous_saved = self._saved_data
        self.data, baseline = self._load(store)
        try:
            replayed = set()
            for financials, checked, require_complete in self._buffer:
                key = company_key(financials)
                entry = self.data['companies'].get(key)
                prior = _utc_timestamp(_checked_at(entry)) if isinstance(entry, dict) else None
                if prior is not None:
                    prior = prior.replace(microsecond=0)
                    if checked < prior or (checked == prior and key not in replayed):
                        logger.debug(
                            '%s: skipped buffered observation at %s against %s (%s)',
                            key,
                            checked.isoformat(),
                            prior.isoformat(),
                            'older timestamp' if checked < prior else 'persisted timestamp tie',
                        )
                        continue
                self._record(financials, checked, require_complete=require_complete)
                replayed.add(key)
            changes = []
            if current is not None:
                financials, checked, require_complete = current
                key = company_key(financials)
                if key in replayed:
                    prior = _utc_timestamp(_checked_at(self.data['companies'][key]))
                    if prior is not None and checked < prior:
                        logger.debug(
                            '%s: skipped observation at %s against %s (older timestamp)',
                            key,
                            checked.isoformat(),
                            prior.isoformat(),
                        )
                        current = None
                if current is not None:
                    changes = self._record(financials, checked, require_complete=require_complete)
            if replayed or current is not None:
                self._save(store, baseline)
            else:
                self._buffer.clear()
                self._saved_data = deepcopy(self.data)
            return changes
        except BaseException:
            if self._saved_data is previous_saved:
                self.data = previous_data
            raise

    def _record(self, financials, checked, *, require_complete):
        key = company_key(financials)
        entry = self.data['companies'].get(key)
        now = checked.isoformat()

        previous = self._previous(key, entry) if entry else None
        if require_complete and (
            not financials.company_id
            or not has_financial_data(financials)
            or (
                previous is not None
                and any(
                    getattr(previous, field) is not None and getattr(financials, field) is None
                    for field in HEADLINES
                )
            )
        ):
            raise Unparseable('Tracxn financials were incomplete. Existing history was kept.')
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
