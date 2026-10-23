from __future__ import annotations

import logging
import os
import re
from contextlib import suppress
from datetime import datetime, timezone

import httpx

from .. import log
from ..errors import AuthFailed, Blocked, NotFound, QuotaExhausted, SourceError, Unparseable
from ..infra.http import HttpClient
from ..interaction import Interaction, Terminal
from .models import CompanyFinancials, FundingRound
from .parsing import (
    clean_name,
    embedded_json,
    first,
    labelled,
    parse_money,
    text_of,
    to_money,
    walk,
)

logger = logging.getLogger(__name__)

# Tracxn publishes no SDK in any registry; its API is distributed as a Postman
# collection (postman.com/tracxnapi/tracxn-api), which is what these calls follow.
API = 'https://platform.tracxn.com/api/3.0'
API_LEGACY = 'https://platform.tracxn.com/api/2.2'
WEB = 'https://tracxn.com'
PAGE_SIZE = 20  # Tracxn's hard maximum per request

# https://tracxn.com/d/companies/zomato/__DV-ekVOT4DO1dVZeaG1vMa1V9Rpnz0bXTg6gdjgxYVk
PROFILE_URL = re.compile(
    r'^(?P<base>https?://(?:www\.)?tracxn\.com/d/companies/(?P<slug>[^/]+)/'
    r'(?P<id>__[^/?#]+))',
    re.IGNORECASE,
)
ENTITY_ID = re.compile(r'^[0-9a-f]{24}$')
DOMAIN = re.compile(
    r'^(?:https?://)?(?:www\.)?(?P<domain>[a-z0-9-]+(?:\.[a-z0-9-]+)+)/?$', re.IGNORECASE
)

# Tracxn publishes request filters but not response fields, and omits keys it has
# no value for. These are the names seen in its filters, sorts and profile pages;
# the record is walked for them rather than read from a fixed path.
NAME_KEYS = ('name', 'companyName')
TOTAL_KEYS = ('totalMoneyRaised', 'totalFunding', 'totalFundingAmount', 'totalAmountRaised')
STAGE_KEYS = ('companyStage', 'stage')
EMPLOYEE_KEYS = ('employeeCount', 'employeeCountRange', 'employeeRange', 'totalEmployeeCount')
VALUATION_KEYS = ('latestValuation', 'valuation', 'postMoneyValuation')
REVENUE_KEYS = ('annualRevenue', 'latestRevenue', 'revenue')

ROUND_NAME_KEYS = (
    'fundingRoundCategory',
    'roundCategory',
    'fundingRoundType',
    'roundName',
    'roundType',
    'series',
)
ROUND_AMOUNT_KEYS = (
    'fundingAmount',
    'roundAmount',
    'amountRaised',
    'fundingRoundAmount',
    'totalAmount',
    'amount',
)
ROUND_DATE_KEYS = (
    'fundingRoundDate',
    'fundingDate',
    'roundDate',
    'announcedDate',
    'dealDate',
    'date',
)
ROUND_VALUATION_KEYS = ('postMoneyValuation', 'postMoneyValuationAmount', 'valuation')
LEAD_KEYS = ('leadInvestors', 'leadInvestorList', 'leadRoundFacilitators', 'leadInvestor')
INVESTOR_COUNT_KEYS = ('investorCount', 'numberOfInvestors', 'totalInvestors')

YEAR_KEYS = ('year', 'financialYear', 'fiscalYear', 'period', 'date')


def to_iso(value):
    """Tracxn dates arrive as dd/mm/yyyy in filters, and variously elsewhere."""
    if isinstance(value, dict):
        year, month, day = value.get('year'), value.get('month'), value.get('day')
        if isinstance(year, int):
            return '{:04d}-{:02d}-{:02d}'.format(year, month or 1, day or 1) if month else str(year)
        value = first(value, 'date', 'value', 'timestamp')
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value > 10**11:  # epoch milliseconds
            value /= 1000.0
        try:
            return datetime.fromtimestamp(value, timezone.utc).date().isoformat()
        except (ValueError, OSError, OverflowError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    match = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{4})$', value)
    if match:
        day, month, year = (int(part) for part in match.groups())
        return '{:04d}-{:02d}-{:02d}'.format(year, month, day)
    if re.match(r'^\d{4}-\d{2}-\d{2}', value):
        return value[:10]
    for pattern in ('%b %d, %Y', '%d %b %Y', '%B %d, %Y', '%d %B %Y', '%b %Y', '%B %Y'):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            continue
    return value


class TracxnClient:
    """Company funding, valuation and revenue from Tracxn.

    With an API token (``TRACXN_API_KEY`` or ``api_key=``) this uses Tracxn's
    REST API: a name, domain or entity id is resolved to a company, then its
    record, funding rounds and yearly valuation/revenue series are pulled. Every
    entity returned costs a credit, so rounds are fetched only up to
    ``max_rounds``.

    Without a token it reads a public profile page in a persistent Playwright
    profile. Tracxn's search is behind its login, so in that mode the company
    has to be given as a profile url (``https://tracxn.com/d/companies/<name>/__<id>``).
    """

    def __init__(
        self,
        api_key=None,
        profile_dir='.tx_profile',
        login=False,
        headless=True,
        timeout=30.0,
        delay=1.0,
        retries=3,
        client=None,
        interaction: Interaction | None = None,
    ):
        # asked to wait while a person clears the bot check in a --login run
        self.interaction = interaction or Terminal()
        self.api_key = api_key if api_key is not None else os.environ.get('TRACXN_API_KEY')
        # masked in every log line from here on (ASVS 16.2.5)
        log.register_secret(self.api_key)
        self.profile_dir = profile_dir
        self.login = login
        self.headless = False if login else headless
        self.timeout = timeout
        self.delay = delay
        self.retries = retries
        self._client = client
        self._http: HttpClient | None = None

    def accepts(self, company: str) -> bool:
        """Whether Tracxn can look this up: anything but a profile url on crunchbase.com."""
        return 'crunchbase.com' not in company.lower()

    def fetch(self, company, max_rounds=20):
        """Every failure leaves as a SourceError, so a caller trying several
        sources can report this one and carry on with the rest."""
        try:
            financials = self._fetch(company, max_rounds)
        except httpx.HTTPStatusError as error:
            raise SourceError(f'Tracxn answered HTTP {error.response.status_code}') from error
        except httpx.HTTPError as error:
            raise SourceError(f'could not reach Tracxn: {error}') from error
        logger.info(
            f'tracxn: {financials.company}: {len(financials.rounds)} round(s), '
            f'{len(financials.notes)} gap(s) noted'
        )
        return financials

    def _fetch(self, company, max_rounds):
        logger.debug(f'tracxn: {company!r} via the {"api" if self.api_key else "web"}')
        if self.api_key:
            financials = self._fetch_api(company, max_rounds)
        elif PROFILE_URL.match(company.strip()):
            financials = self._fetch_web(company.strip(), max_rounds)
        else:
            raise SourceError(
                'Tracxn needs either an API token (TRACXN_API_KEY / --tracxn-key) or a '
                'profile url like https://tracxn.com/d/companies/<name>/__<id>, got {!r}. '
                'Its company search is behind a login, so names cannot be resolved '
                'without the API.'.format(company)
            )
        financials.fill_from_rounds()
        return financials

    def close(self) -> None:
        """Release the API connection pool, if one was opened (and not handed
        in). Safe to call more than once; whoever makes a client closes it."""
        if self._http is not None:
            self._http.close()
            self._http = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    # -- api --------------------------------------------------------------

    @property
    def http(self) -> HttpClient:
        """Made on first use: a run on the website never needs one."""
        if self._http is None:
            self._http = HttpClient(
                'Tracxn',
                client=self._client,
                headers={},
                timeout=self.timeout,
                retries=self.retries,
                backoff=self.delay,
                interval=self.delay,
                secret=self.api_key,
            )
        return self._http

    @property
    def client(self) -> httpx.Client:
        return self.http.client

    def _post(self, path, body, base=API):
        # no Retry-After is sent, so HttpClient's own backoff is all there is;
        # a rate limit that outlasts it leaves as Blocked
        response = self.http.post(
            base + path,
            json=body,
            headers={'accessToken': self.api_key or '', 'Content-Type': 'application/json'},
        )

        if response.status_code == 401:
            raise AuthFailed('Tracxn rejected the API token')
        if response.status_code == 403:
            message = _error(response)
            if 'credit' in message.lower():
                raise QuotaExhausted(
                    'Tracxn API is out of credits - retrying cannot help until they are renewed'
                )
            raise AuthFailed(
                'Tracxn refused the request (token expired, or the plan does '
                'not cover it): {}'.format(message)
            )
        if response.status_code == 400:
            raise SourceError('Tracxn rejected the request: {}'.format(_error(response)))
        response.raise_for_status()
        try:
            return response.json()
        except ValueError:
            raise Unparseable(
                'Tracxn returned something other than JSON for {}'.format(path)
            ) from None

    def _fetch_api(self, company, max_rounds):
        entity_id, record = self._resolve(company)
        payload = self._post('/companies', {'view': 'companyData', 'filter': {'id': [entity_id]}})
        record = self._record(payload, entity_id) or record

        financials = CompanyFinancials(source='tracxn', company=company, company_id=entity_id)
        if record:
            self._read_company(record, financials)

        rounds = []
        start = 0
        # only a short page shows the list ran out; stopping at max_rounds does not
        ran_out = False
        while len(rounds) < max_rounds:
            size = min(PAGE_SIZE, max_rounds - len(rounds))
            try:
                payload = self._post(
                    '/fundingrounds',
                    {'filter': {'companiesId': [entity_id]}, 'from': start, 'size': size},
                )
            except AuthFailed as error:
                logger.warning(f'tracxn: funding rounds unavailable: {error}')
                financials.notes.append('funding rounds: {}'.format(error))
                break
            page = self._rounds(payload)
            rounds.extend(page)
            if len(page) < size:
                ran_out = True
                break
            start += size
        financials.rounds = _dedupe(rounds)[: max(0, max_rounds)]
        financials.rounds_cut(not ran_out)

        for metric, attribute in (('valuation', 'valuation'), ('revenue', 'revenue')):
            try:
                payload = self._post(
                    '/timeseries/{}'.format(metric),
                    {'filter': {'companyId': [entity_id]}, 'from': 0},
                )
            except (AuthFailed, SourceError) as error:
                logger.info(f'tracxn: {metric} series unavailable: {error}')
                financials.notes.append('{}: {}'.format(metric, error))
                continue
            money, year = self._latest_point(payload)
            if money is not None:
                setattr(financials, attribute, money)
                setattr(
                    financials, attribute + ('_date' if metric == 'valuation' else '_year'), year
                )
        return financials

    def _resolve(self, company):
        """-> (entity id, the matching record if the lookup returned one)."""
        text = company.strip()
        if ENTITY_ID.match(text):
            return text, None

        profile = PROFILE_URL.match(text)
        domain = DOMAIN.match(text) if not profile else None
        if domain:
            payload = self._post(
                '/companies', {'filter': {'domain': [domain.group('domain').lower()]}, 'size': 1}
            )
            wanted = None
        else:
            wanted = clean_name(profile.group('slug').replace('-', ' ') if profile else text)
            payload = self._post(
                '/companies/search', {'filter': {'companyName': wanted}, 'size': 5}, base=API_LEGACY
            )

        candidates = [
            node
            for node in walk(payload)
            if isinstance(node.get('id'), str)
            and ENTITY_ID.match(node['id'])
            and first(node, *NAME_KEYS)
        ]
        if not candidates:
            raise NotFound('Tracxn has no company matching {!r}'.format(company))
        if wanted:
            for node in candidates:
                if clean_name(str(first(node, *NAME_KEYS))).lower() == wanted.lower():
                    return node['id'], node
        return candidates[0]['id'], candidates[0]

    def _record(self, payload, entity_id):
        fallback = None
        for node in walk(payload):
            if entity_id and node.get('id') == entity_id:
                return node
            if (
                fallback is None
                and first(node, *NAME_KEYS)
                and any(key in node for key in (*TOTAL_KEYS, 'domain', 'foundedYear'))
            ):
                fallback = node
        return fallback

    def _rounds(self, payload):
        return [item for item in (self._read_round(node) for node in walk(payload)) if item]

    def _latest_point(self, payload):
        """The newest (money, year) in a yearly series. Tracxn gives raw values, no growth."""
        points = []
        for node in walk(payload):
            year = first(node, *YEAR_KEYS)
            if year is None or isinstance(year, (dict, list)):
                continue
            money = to_money(
                {
                    key: node[key]
                    for key in node
                    if key in ('value', 'amount', 'currency', 'amountInUSD', 'currencyCode')
                }
            )
            if money is None:
                continue
            points.append((str(year), money))
        if not points:
            return None, None
        year, money = max(points, key=lambda point: point[0])
        return money, year

    # -- web --------------------------------------------------------------

    def _fetch_web(self, url, max_rounds):
        from ..infra.browser import browser, looks_blocked

        match = PROFILE_URL.match(url)
        if match is None:
            raise SourceError('not a Tracxn profile url: {!r}'.format(url))
        base = match.group('base')
        payloads, texts = [], []

        def capture(response):
            if 'tracxn.com' not in response.url or '/api/' not in response.url:
                return
            with suppress(Exception):
                payloads.append(response.json())

        try:
            with browser(
                self.profile_dir, headless=self.headless, timeout=int(self.timeout * 1000)
            ) as page:
                page.on('response', capture)
                for target in (base, base + '/funding-and-investors'):
                    response = page.goto(target, wait_until='domcontentloaded')
                    if response is not None and response.status == 404:
                        if target == base:
                            raise NotFound('no Tracxn profile at {}'.format(base))
                        continue
                    self._settle(page, looks_blocked)
                    payloads.extend(embedded_json(page.content()))
                    with suppress(Exception):
                        texts.append(page.inner_text('body'))
        except SourceError:
            raise
        # navigation failures, timeouts and a closed browser all surface as plain
        # playwright errors, and each should read as this source failing
        except Exception as error:
            raise SourceError(
                'could not load {}: {}'.format(base, str(error).split('\n')[0][:160])
            ) from error

        financials = CompanyFinancials(
            source='tracxn',
            url=base,
            company_id=match.group('id'),
            company=match.group('slug').replace('-', ' ').title(),
        )
        for payload in payloads:
            record = self._record(payload, None)
            if record:
                self._read_company(record, financials)
                break
        rounds = []
        for payload in payloads:
            rounds.extend(self._rounds(payload))
        found = _dedupe(rounds)
        financials.rounds = found[: max(0, max_rounds)]
        financials.rounds_cut(len(found) > len(financials.rounds))
        self._from_text('\n'.join(texts), financials)

        if (
            financials.total_funding is None
            and not financials.rounds
            and financials.valuation is None
            and financials.revenue is None
        ):
            raise Unparseable(
                'no funding data found on {} - the page layout may have changed, '
                'or it needs a signed in profile (--login)'.format(base)
            )
        return financials

    def _settle(self, page, looks_blocked):
        with suppress(Exception):
            page.wait_for_load_state('networkidle', timeout=int(self.timeout * 1000))
        if self.login:
            self.interaction.pause(
                'A browser window is open. Clear any bot check and sign in to Tracxn if '
                'you want to, then come back here.'
                ' Press Enter once the company page is visible.'
            )
            self.login = False
            return
        if looks_blocked(page):
            raise Blocked(
                'Tracxn served a bot check instead of the company page. Re-run with --login '
                'to clear it once in a visible browser; the saved profile is reused '
                'headlessly afterwards.'
            )

    # -- parsing ----------------------------------------------------------

    def _read_company(self, record, financials):
        name = text_of(first(record, *NAME_KEYS))
        if name:
            financials.company = name
        if isinstance(record.get('id'), str) and ENTITY_ID.match(record['id']):
            financials.company_id = record['id']

        simple = (
            ('website', ('domain', 'website', 'websiteUrl')),
            ('founded', ('foundedYear', 'foundingYear', 'foundingDate')),
            ('stage', STAGE_KEYS),
            ('employees', EMPLOYEE_KEYS),
            ('stock_symbol', ('stockTicker', 'ticker', 'stockSymbol')),
            ('description', ('shortDescription', 'description')),
            ('url', ('tracxnUrl', 'tracxnProfileUrl', 'profileUrl')),
        )
        for attribute, keys in simple:
            if getattr(financials, attribute) is None:
                value = text_of(first(record, *keys))
                if value:
                    setattr(financials, attribute, value)

        if financials.total_funding is None:
            financials.total_funding = to_money(first(record, *TOTAL_KEYS))
        if financials.valuation is None:
            financials.valuation = to_money(first(record, *VALUATION_KEYS))
        if financials.revenue is None:
            financials.revenue = to_money(first(record, *REVENUE_KEYS))
        if financials.funding_rounds_count is None:
            count = first(
                record, 'totalFundingRounds', 'fundingRoundCount', 'numberOfFundingRounds'
            )
            if isinstance(count, int):
                financials.funding_rounds_count = count

    def _read_round(self, node):
        name = text_of(first(node, *ROUND_NAME_KEYS))
        if not isinstance(name, str):
            return None
        date = to_iso(first(node, *ROUND_DATE_KEYS))
        amount = to_money(first(node, *ROUND_AMOUNT_KEYS))
        if date is None and amount is None:
            return None
        if any(key in node for key in (*TOTAL_KEYS, 'foundedYear')):
            return None  # a company record that happens to carry a stage

        leads = []
        for lead in _as_list(first(node, *LEAD_KEYS)):
            text = text_of(lead)
            if text:
                leads.append(text)
        count = first(node, *INVESTOR_COUNT_KEYS)
        if count is None and isinstance(node.get('investors'), list):
            count = len(node['investors'])

        return FundingRound(
            date=date,
            round=name,
            amount=amount,
            post_money_valuation=to_money(first(node, *ROUND_VALUATION_KEYS)),
            lead_investors=leads,
            investor_count=count if isinstance(count, int) else None,
        )

    def _from_text(self, text, financials):
        if financials.total_funding is None:
            financials.total_funding = parse_money(labelled(text, 'Total Funding') or '')
        if financials.valuation is None:
            financials.valuation = parse_money(
                labelled(text, 'Latest Valuation', 'Post Money Valuation', 'Valuation') or ''
            )
        if financials.revenue is None:
            financials.revenue = parse_money(labelled(text, 'Annual Revenue', 'Revenue') or '')
        if financials.stage is None:
            financials.stage = labelled(text, 'Company Stage', 'Stage')
        if financials.employees is None:
            financials.employees = labelled(text, 'Employee Count', 'Employees')
        if financials.founded is None:
            financials.founded = labelled(text, 'Founded Year', 'Founded')
        if financials.last_funding_type is None:
            latest = labelled(text, 'Latest Funding Round', 'Latest Round')
            if latest:
                financials.last_funding_type = latest.split(',')[0].strip()


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _dedupe(rounds):
    seen = {}
    for item in rounds:
        key = item.key()
        if key not in seen or (item.amount is not None and seen[key].amount is None):
            seen[key] = item
    return sorted(seen.values(), key=lambda r: r.date or '', reverse=True)


def _error(response):
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict):
        return str(first(body, 'message', 'error', 'errorCode') or body)[:200]
    return str(body)[:200]
