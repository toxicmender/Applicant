"""Attributed WWR public RSS feed; absence from a feed is not closure."""

import re
from datetime import timezone
from email.utils import parsedate_to_datetime
from html import unescape
from xml.etree import ElementTree

from ..domain.capability import Capability, Field
from ..domain.job import Job
from ..domain.places import within
from ..errors import Blocked, Unparseable
from ..infra.http import HttpClient

FEED = 'https://weworkremotely.com/remote-jobs.rss'


class WeWorkRemotely:
    capability = Capability(publishes=frozenset({Field.ID, Field.URL, Field.POSTED, Field.REMOTE}))

    def __init__(self, *, client=None, headless=True):
        self.http = HttpClient('weworkremotely', client=client)
        self.raw = b''

    def search(self, keywords, location='', limit=25, posted_within_days=None):
        response = self.http.get(FEED)
        if response.status_code == 403:
            raise Blocked('weworkremotely: access denied')
        response.raise_for_status()
        self.raw = response.content
        return self.parse(self.raw, keywords, location)[:limit]

    @staticmethod
    def parse(body, keywords='', location=''):
        if len(body) > 8_000_000 or b'<!ENTITY' in body.upper() or b'<!DOCTYPE' in body.upper():
            raise Unparseable('unsafe or oversized RSS document')
        try:
            root = ElementTree.fromstring(body)
        except ElementTree.ParseError:
            raise Unparseable('invalid WWR RSS') from None
        if root.tag != 'rss' or root.find('channel') is None:
            raise Unparseable('expected RSS channel')
        result = []
        for item in root.findall('./channel/item'):
            title, url = item.findtext('title', ''), item.findtext('link', '')
            if not title or not url.startswith('https://weworkremotely.com/remote-jobs/'):
                raise Unparseable('RSS job requires a title and attributed posting link')
            description = unescape(re.sub('<[^>]+>', ' ', item.findtext('description', '')))
            if not all(
                word.casefold() in (title + ' ' + description).casefold()
                for word in keywords.split()
            ):
                continue
            # A feed's "remote" label does not establish worldwide eligibility.
            region = item.findtext('region') or None
            worldwide = (region or '').strip().casefold() in (
                'anywhere in the world',
                'worldwide',
                'global',
            )
            eligible = True if worldwide or not location else within(location, region)
            if eligible is False:
                continue
            company, sep, role = title.partition(': ')
            date = item.findtext('pubDate')
            try:
                posted = (
                    parsedate_to_datetime(date).astimezone(timezone.utc).date().isoformat()
                    if date
                    else None
                )
            except (ValueError, TypeError):
                posted = None
            result.append(
                Job(
                    source='weworkremotely',
                    id=item.findtext('guid') or url,
                    title=role if sep else title,
                    company=company if sep else None,
                    url=url,
                    location=region,
                    remote=True,
                    posted=posted,
                    flags=['location_unresolved'] if location and eligible is None else [],
                )
            )
        return result

    def close(self):
        self.http.close()
