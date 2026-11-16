"""Private verified company retrieval metadata, separate from user exports."""

import hashlib
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..domain.canonical import content_hash
from ..files import read_document, write_document
from ..financials.tracker import _utc_timestamp
from ..infra.store.financials_lock import export_lock

logger = logging.getLogger(__name__)


class CacheClock:
    def __init__(self, wall=None, monotonic=time.monotonic):
        self.wall = wall or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic
        self.anchor = self.wall(), self.monotonic()

    def current(self):
        anchor, tick = self.anchor
        live = self.wall()
        expected = anchor + timedelta(seconds=self.monotonic() - tick)
        adjustment = expected - live
        if timedelta(seconds=1) <= adjustment <= timedelta(minutes=15):
            logger.debug('cache clock adjusted for a backward wall-clock jump of %s', adjustment)
            return expected
        if adjustment > timedelta(minutes=15):
            logger.warning('cache clock moved backwards by %s; check system time', adjustment)
        if abs(adjustment) >= timedelta(seconds=1):
            self.anchor = live, self.monotonic()
        return live

    def fresh(self, stamp, days):
        checked = _utc_timestamp(stamp)
        if checked is None:
            logger.warning('company cache timestamp is invalid or timezone-naive; refreshing')
            return False
        age = self.current() - checked
        if age < -timedelta(minutes=2):
            logger.warning('company cache timestamp %s is in the future; check system time', stamp)
            return False
        if age < timedelta():
            logger.debug('company cache treating small future timestamp %s as age zero', stamp)
        return age < timedelta(days=days)


def browser_scope(profile):
    if profile is None:
        return 'public'
    path = Path(profile).resolve()
    state = path / 'storage_state.json'
    try:
        generation = hashlib.sha256(state.read_bytes()).hexdigest()
    except FileNotFoundError:
        return 'public' if not path.exists() else None
    return content_hash([str(path), generation])


def newer_retrieval(current, stamp):
    old = _utc_timestamp(current.get('retrieved_at'))
    new = _utc_timestamp(stamp)
    return new is not None and (old is None or new > old)


class CompanyCache:
    def __init__(self, output, *, clock=None):
        target = Path(output).resolve()
        self.path = target.with_name(f'.{target.name}.company-cache.json')
        self.clock = clock or CacheClock()

    def entry(self, provider, locator, scope):
        return read_document(self.path).get(content_hash([provider, locator, scope]))

    def locked(self):
        return export_lock(self.path)

    def keep(
        self, provider, locator, scope, identity, previous, coverage, payload, stamp, *, commit=None
    ):
        with self.locked():
            return self.keep_locked(
                provider,
                locator,
                scope,
                identity,
                previous,
                coverage,
                payload,
                stamp,
                commit=commit,
            )

    def keep_locked(
        self, provider, locator, scope, identity, previous, coverage, payload, stamp, *, commit=None
    ):
        document = read_document(self.path)
        key = content_hash([provider, locator, scope])
        current = document.get(key)
        if current is not None:
            if current['identity'] != identity:
                logger.warning(
                    '%s: verified cache identity changed; keeping existing entry', provider
                )
                return False
            if current != previous or not newer_retrieval(current, stamp):
                logger.debug(
                    '%s: keeping existing cache revision %s', provider, current['revision']
                )
                return False
            if current['coverage'] > coverage:
                return False
        if commit is not None:
            commit()
        document[key] = {
            'identity': identity,
            'revision': 1 if current is None else current['revision'] + 1,
            'retrieved_at': stamp,
            'coverage': coverage,
            'payload': payload,
        }
        write_document(self.path, document)
        return True
