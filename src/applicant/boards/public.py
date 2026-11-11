"""Bounded public JSON-LD adapters; missing structured content is explicit failure."""

import json
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from ..domain.capability import Capability, Field
from ..domain.job import Job
from ..errors import AuthFailed, Blocked, Unparseable
from ..infra.http import HttpClient

SITES = {
    'wellfound': ('https://wellfound.com/jobs', '/jobs/'),
    'builtin': ('https://builtin.com/jobs/remote', '/job/'),
    'welcometothejungle': ('https://www.welcometothejungle.com/en/jobs', '/jobs/'),
    'arc': ('https://arc.dev/remote-jobs', '/remote-jobs/'),
    'instahyre': ('https://www.instahyre.com/search-jobs/', '/job/'),
}


class StructuredHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts, self.links, self._data = [], [], None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script' and (attrs.get('type') or '').lower() == 'application/ld+json':
            self._data = []
        if tag == 'a' and attrs.get('href'):
            self.links.append(attrs['href'])

    def handle_data(self, data):
        if self._data is not None:
            self._data.append(data)

    def handle_endtag(self, tag):
        if tag == 'script' and self._data is not None:
            self.scripts.append(''.join(self._data))
            self._data = None


def structured_records(text, kind):
    parser = StructuredHTML()
    parser.feed(text)
    records = []

    def walk(value):
        if isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            types = value.get('@type', [])
            if kind in ([types] if isinstance(types, str) else types):
                records.append(value)
            for key in ('@graph', 'itemListElement', 'item'):
                if key in value:
                    walk(value[key])

    for script in parser.scripts:
        try:
            walk(json.loads(script))
        except (ValueError, TypeError):
            continue
    return records, parser.links


class PublicBoard:
    """Public structured-data capture, never a login or challenge bypass."""

    capability = Capability(
        publishes=frozenset({Field.ID, Field.URL, Field.POSTED, Field.SALARY, Field.REMOTE})
    )

    def __init__(self, name, *, client=None, headless=True):
        self.name = name
        self.http = HttpClient(name, client=client, retries=3)
        self.raw = {}
        self.requisition_metadata = {}
        self.source_urls = {}
        self.structured_jobs = {}
        self.partial_jobs = []

    def _read(self, url):
        base = SITES[self.name][0]
        host = (urlsplit(base).hostname or '').removeprefix('www.')
        parsed = urlsplit(url)
        if (
            parsed.scheme != 'https'
            or (parsed.hostname or '').removeprefix('www.') != host
            or parsed.username
            or parsed.password
        ):
            raise ValueError('public capture URL must belong to the selected provider')
        response = self.http.get(url)
        if response.status_code == 403 or any(
            x in response.text[:8000].casefold()
            for x in ('cf-chl-', 'captcha-form', 'just a moment...', 'access denied')
        ):
            raise Blocked(f'{self.name}: access denied or challenge')
        if response.status_code == 401 or any(
            x in str(response.url) for x in ('/login', '/signin', '/authwall')
        ):
            raise AuthFailed(f'{self.name}: authentication required')
        if response.url.scheme != 'https' or response.url.host.removeprefix('www.') != host:
            raise Unparseable(f'{self.name}: listing redirected outside the selected board')
        if response.status_code >= 400:
            raise Unparseable(f'{self.name}: HTTP {response.status_code}; not closure evidence')
        if len(response.content) > 8_000_000:
            raise Unparseable('public page exceeds capture limit')
        self.raw[url] = response.content
        if sum(map(len, self.raw.values())) > 16_000_000:
            raise Unparseable('public acquisition exceeds 16 MB capture budget')
        return response.text

    def capture(self, url):
        return self.parse_listing(self._read(url), url)

    def parse_listing(self, body, url):
        records, _ = structured_records(body, 'JobPosting')
        if not records:
            raise Unparseable(f'{self.name}: no public structured JobPosting; use research import')
        return [self._job(record, url) for record in records]

    def _job(self, record, url):
        title = record.get('title')
        company = record.get('hiringOrganization')
        if (
            not isinstance(title, str)
            or not isinstance(company, dict)
            or not isinstance(company.get('name'), str)
        ):
            raise Unparseable('JobPosting requires title and structured hiring organization')
        identifier = record.get('identifier')
        identifier = identifier.get('value') if isinstance(identifier, dict) else identifier
        posting = record.get('url') or url
        source_id = str(identifier) if identifier is not None else posting
        self.source_urls[source_id] = url
        self.structured_jobs[source_id] = record
        locations = record.get('jobLocation', [])
        locations = locations if isinstance(locations, list) else [locations]
        cities = []
        for location in locations:
            address = location.get('address', {}) if isinstance(location, dict) else {}
            if isinstance(address, dict):
                cities.append(
                    ', '.join(
                        str(address[k])
                        for k in ('addressLocality', 'addressRegion', 'addressCountry')
                        if address.get(k)
                    )
                )
        salary = record.get('baseSalary')
        salary_text = None
        if isinstance(salary, dict) and isinstance(salary.get('value'), dict):
            value = salary['value']
            salary_text = f'{salary.get("currency", "")} {value.get("minValue", value.get("value", ""))}-{value.get("maxValue", value.get("value", ""))} {value.get("unitText", "")}'
        posted = record.get('datePosted')
        job = Job(
            source=self.name,
            id=source_id,
            title=title,
            company=company['name'],
            url=posting,
            location='; '.join(cities) or None,
            posted=posted[:10] if isinstance(posted, str) else None,
            salary=salary_text,
            remote=True if record.get('jobLocationType') == 'TELECOMMUTE' else None,
            employment_type=str(record['employmentType']) if record.get('employmentType') else None,
        )
        # External requisition URLs may establish identity; names alone never do.
        external = urlsplit(posting)
        if (
            external.scheme == 'https'
            and external.hostname
            and external.hostname != urlsplit(url).hostname
            and not external.username
        ):
            self.requisition_metadata[source_id] = {'employer_requisition_url': posting}
        return job

    def search(self, keywords, location='', limit=25, posted_within_days=None):
        landing, pattern = SITES[self.name]
        body = self._read(landing)
        records, links = structured_records(body, 'JobPosting')
        jobs = [self._job(record, landing) for record in records]
        if not jobs:
            candidates = list(dict.fromkeys(urljoin(landing, x) for x in links if pattern in x))[
                : min(25, limit * 2)
            ]
            for url in candidates:
                try:
                    jobs.extend(self.capture(url))
                    self.partial_jobs = jobs.copy()
                except Unparseable:
                    continue
                if len(jobs) >= limit:
                    break
        if not jobs:
            raise Unparseable(
                f'{self.name}: public structured discovery unavailable; import evidence instead'
            )
        return [
            job
            for job in jobs
            if all(word.casefold() in job.title.casefold() for word in keywords.split())
            and (not location or not job.location or location.casefold() in job.location.casefold())
        ][:limit]

    def close(self):
        self.http.close()
