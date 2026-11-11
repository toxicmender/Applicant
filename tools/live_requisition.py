"""Verify an explicitly selected pair of live listings."""

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from applicant.boards.public import SITES, PublicBoard
from applicant.domain.contracts import now
from applicant.domain.providers import PROVIDERS
from applicant.errors import AuthFailed, Blocked, ConfigError, SourceError, Unparseable
from applicant.files import write_document
from applicant.infra.browser import BrowserSession, looks_blocked
from applicant.infra.owned_worker import run_worker
from applicant.infra.profile_lock import profile_lock
from applicant.services.provider_setup import import_session, session_path
from applicant.services.research_feed import collect_feed
from applicant.settings import Settings


def host(url):
    return (urlsplit(url).hostname or '').removeprefix('www.')


def candidate(value):
    if (
        not isinstance(value, dict)
        or set(value) != {'listings'}
        or not isinstance(value['listings'], dict)
        or len(value['listings']) != 2
    ):
        raise ValueError('candidate requires exactly two canonical listing URLs')
    for name, url in value['listings'].items():
        if name not in {*SITES, 'linkedin'} or not isinstance(url, str):
            raise ValueError('candidate uses an unsupported listing adapter')
        parsed = urlsplit(url)
        prefix = '/jobs/view/' if name == 'linkedin' else SITES[name][1]
        if (
            parsed.scheme != 'https'
            or host(url) != host(PROVIDERS[name].url)
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
            or parsed.query
            or parsed.fragment
            or prefix not in parsed.path
            or not parsed.path.split(prefix, 1)[1]
            or (name == 'linkedin' and not parsed.path.startswith(prefix))
        ):
            raise ValueError('candidate requires a provider-owned HTTPS listing URL')
    return dict(sorted(value['listings'].items()))


def _linkedin_page(connection, arguments):
    url, state = arguments
    with BrowserSession(storage_state=state) as browser:
        try:
            page = browser.start()
            response = page.goto(url, wait_until='domcontentloaded')
            if looks_blocked(page) or (response is not None and response.status == 403):
                return 'blocked', ''
            if host(page.url) != host(url) or not urlsplit(page.url).path.startswith('/jobs/view/'):
                return 'needs_setup', ''
            if response is None or response.status != 200:
                return 'unreadable', ''
            body = page.content()
            if len(body.encode('utf-8')) > 8_000_000:
                return 'unreadable', ''
            return 'success', body
        finally:
            connection.send(('closing', None))


class ListingCapture(PublicBoard):
    def __init__(self, name, urls, state):
        super().__init__(name)
        self.url, self.state = urls[name], state

    def search(self, keywords, location='', limit=25, posted_within_days=None):
        if self.name == 'linkedin':
            status, body = run_worker(_linkedin_page, (self.url, self.state))
            if status == 'blocked':
                raise Blocked('LinkedIn listing blocked')
            if status == 'needs_setup':
                raise AuthFailed('LinkedIn session required')
            if status != 'success':
                raise Unparseable('LinkedIn listing unavailable')
            jobs = self.parse_listing(body, self.url)
        else:
            jobs = self.capture(self.url)
        if len(jobs) != 1:
            raise Unparseable('candidate URL must yield one listing')
        # A link to another catalog board is not an employer requisition.
        for metadata in self.requisition_metadata.values():
            employer_url = metadata.get('employer_requisition_url')
            if employer_url and host(employer_url) in {host(p.url) for p in PROVIDERS.values()}:
                metadata.pop('employer_requisition_url')
        return jobs


def verdict(feed, urls, sha):
    outcomes = feed['outcomes']
    observations = [
        {
            'provider': x['job']['source'],
            'source_url': urls[x['job']['source']],
            'acquired_at': x['acquired_at'],
            'content_sha256': x['content_sha256'],
            'content_representation': x['content_representation'],
            'job_version_id': x['job_version_id'],
            'origin': x['origin'],
            'syndication': 'reported' if x.get('syndication_origin') else 'unknown',
        }
        for x in feed['observations']
    ]
    group = next(
        (
            g
            for g in feed['groups']
            if g['status'] == 'confirmed'
            and set(g['sources']) == set(urls)
            and set(g['job_version_ids']) == {x['job_version_id'] for x in observations}
        ),
        None,
    )
    blocked = len(outcomes) != 2 or any(x['status'] != 'success' for x in outcomes)
    status = 'blocked' if blocked else 'verified' if group and len(observations) == 2 else 'failed'
    return {
        'kind': 'live_requisition_validation',
        'schema_version': 1,
        'checked_at': now().isoformat(),
        'tested_sha': sha,
        'status': status,
        'reason': {
            'blocked': 'candidate acquisitions did not both succeed',
            'verified': 'both live listings identify the same employer requisition',
            'failed': 'employer requisition identity could not be confirmed',
        }[status],
        'outcomes': [{k: x[k] for k in ('provider', 'status')} for x in outcomes],
        'observations': observations,
        'match_revision': group['match_revision'] if group else None,
        'direct_sources': group['direct_sources'] if group else [],
        'independent_corroboration': False,
    }


def release_eligible(report, sha, *, clock=now):
    if (
        report.get('kind') != 'live_requisition_validation'
        or report.get('schema_version') != 1
        or report.get('status') != 'verified'
        or report.get('tested_sha') != sha
        or not report.get('match_revision')
    ):
        return False
    try:
        checked = datetime.fromisoformat(report['checked_at'])
    except (KeyError, TypeError, ValueError):
        return False
    return checked.utcoffset() is not None and timedelta(0) <= clock() - checked <= timedelta(
        hours=24
    )


def verify(value, sha, *, session=None, collector=collect_feed):
    urls = candidate(value)
    if not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise ValueError('tested SHA must be a full Git commit ID')
    if session and 'linkedin' not in urls:
        raise ValueError('LinkedIn session requires a LinkedIn candidate')
    with TemporaryDirectory() as folder:
        root = Path(folder)
        settings = Settings(data_dir=root, store='files')

        def collect(state=None):
            return collector(
                root / 'live.db',
                {'providers': list(urls), 'limit': 1, 'require_all_sources': True},
                factory=lambda name, **_: ListingCapture(
                    name, urls, state if name == 'linkedin' else None
                ),
            )

        try:
            if not session:
                feed = collect()
            else:
                source = root / 'session-input.json'
                write_document(source, json.loads(session))
                imported = import_session(settings, 'linkedin', source, verify=True)
                if imported['status'] != 'verified':
                    raise AuthFailed('session verification failed')
                target = session_path(settings, 'linkedin')
                with profile_lock(target.with_suffix('.lock')):
                    feed = collect(json.loads(target.read_text(encoding='utf-8')))
        except (ConfigError, SourceError):
            feed = {
                'outcomes': [{'provider': name, 'status': 'needs_setup'} for name in urls],
                'observations': [],
                'groups': [],
            }
        return verdict(feed, urls, sha)
