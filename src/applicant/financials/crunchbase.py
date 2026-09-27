from __future__ import annotations

import logging
import os
import re
from contextlib import suppress

import httpx

from .. import log
from ..infra.http import USER_AGENT, HttpClient
from ..interaction import Interaction, Terminal
from .errors import AuthError, ChallengeError, CompanyNotFound, FinancialsError, ParseError
from .models import CompanyFinancials, FundingRound
from .parsing import (
    clean_name,
    embedded_json,
    first,
    humanize,
    labelled,
    parse_money,
    text_of,
    to_money,
    walk,
)

logger = logging.getLogger(__name__)

API = 'https://api.crunchbase.com/api/v4'
WEB = 'https://www.crunchbase.com'

# https://www.crunchbase.com/organization/zomato[/company_financials]
ORG_URL = re.compile(r'crunchbase\.com/organization/(?P<permalink>[^/?#]+)', re.IGNORECASE)

# Crunchbase publishes no SDK; these card ids are the ones in its v4 spec. The
# `fields` card returns every property the key's plan allows, which is safer than
# naming them in field_ids - one unknown field id fails the whole request.
CARDS = 'fields,raised_funding_rounds'

# Crunchbase hands out bands as enum ids
REVENUE_RANGES = {
    'r_00000000': 'Less than $1M',
    'r_00001000': '$1M to $10M',
    'r_00010000': '$10M to $50M',
    'r_00050000': '$50M to $100M',
    'r_00100000': '$100M to $500M',
    'r_00500000': '$500M to $1B',
    'r_01000000': '$1B to $10B',
    'r_10000000': '$10B+',
}
EMPLOYEES = re.compile(r'^c_0*(\d+)_(?:0*(\d+)|max)$')

HEADERS = {'User-Agent': USER_AGENT, 'Accept': 'application/json'}


def permalink(company):
    """A Crunchbase url, a permalink, or a name -> the permalink we would try first."""
    match = ORG_URL.search(company)
    if match:
        return match.group('permalink').lower()
    return re.sub(r'[^a-z0-9]+', '-', clean_name(company).lower()).strip('-')


def revenue_band(value):
    return REVENUE_RANGES.get(value, value) if isinstance(value, str) else None


def employee_band(value):
    """'c_01001_05000' -> '1001-5000', 'c_10001_max' -> '10001+'."""
    if not isinstance(value, str):
        return None
    match = EMPLOYEES.match(value)
    if not match:
        return value
    low, high = match.groups()
    return '{}-{}'.format(low, high) if high else '{}+'.format(low)


class CrunchbaseClient:
    """Company funding from Crunchbase.

    With an API key (``CRUNCHBASE_API_KEY`` or ``api_key=``) this uses the v4
    entity lookup, which is exact and needs no browser. Crunchbase's free Basic
    key does not include funding fields; the error says so rather than returning
    an empty record.

    Without a key it reads the public organization page in a persistent
    Playwright profile, the way the Glassdoor client does: crunchbase.com is
    behind Cloudflare, so the first run needs ``login=True`` to clear the check
    once. Logged out pages show headline figures but blur some round details.
    """

    def __init__(
        self,
        api_key=None,
        profile_dir='.cb_profile',
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
        self.api_key = api_key if api_key is not None else os.environ.get('CRUNCHBASE_API_KEY')
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
        """Whether Crunchbase can look this up: anything but a profile url on tracxn.com."""
        return 'tracxn.com' not in company.lower()

    def fetch(self, company, max_rounds=20):
        """Every failure leaves as a FinancialsError, so a caller trying several
        sources can report this one and carry on with the rest."""
        mode = 'api' if self.api_key else 'web'
        logger.debug(f'crunchbase: {company!r} via the {mode}')
        try:
            if self.api_key:
                financials = self._fetch_api(company, max_rounds)
            else:
                financials = self._fetch_web(company, max_rounds)
        except httpx.HTTPStatusError as error:
            raise FinancialsError(
                f'Crunchbase answered HTTP {error.response.status_code}'
            ) from error
        except httpx.HTTPError as error:
            raise FinancialsError(f'could not reach Crunchbase: {error}') from error
        except ValueError as error:  # a response body that was not JSON
            raise ParseError(f'Crunchbase sent unreadable data: {error}') from error
        financials.fill_from_rounds()
        logger.info(
            f'crunchbase: {financials.company}: {len(financials.rounds)} round(s), '
            f'{len(financials.notes)} gap(s) noted'
        )
        return financials

    # -- api --------------------------------------------------------------

    @property
    def http(self) -> HttpClient:
        """Made on first use: a run on the website never needs one."""
        if self._http is None:
            self._http = HttpClient(
                'Crunchbase',
                client=self._client,
                headers=HEADERS,
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

    def _get(self, path, params):
        # a rate limit that outlasts the retries leaves HttpClient as Blocked
        response = self.http.get(
            API + path, params=params, headers={'X-cb-user-key': self.api_key or ''}
        )

        if response.status_code == 401:
            raise AuthError('Crunchbase rejected the API key')
        if response.status_code == 403:
            raise AuthError(
                'this Crunchbase key cannot read funding data - the free Basic '
                'plan does not include it: {}'.format(_error(response))
            )
        if response.status_code == 400:
            raise AuthError(
                'Crunchbase refused the request, usually a card the plan does '
                'not include: {}'.format(_error(response))
            )
        return response

    def _fetch_api(self, company, max_rounds):
        slug = permalink(company)
        params = {'card_ids': CARDS}

        response = self._get('/entities/organizations/{}'.format(slug), params)
        if response.status_code == 404 and not ORG_URL.search(company):
            # a name rather than a permalink - let Crunchbase's own autocomplete resolve it
            slug = self._resolve(company)
            logger.info(f'crunchbase: {company!r} resolved to the permalink {slug!r}')
            response = self._get('/entities/organizations/{}'.format(slug), params)
        if response.status_code == 404:
            raise CompanyNotFound('no Crunchbase organization {!r}'.format(company))
        response.raise_for_status()

        return self._build([response.json()], slug, company, max_rounds)

    def _resolve(self, name):
        response = self._get(
            '/autocompletes',
            {'query': clean_name(name), 'collection_ids': 'organizations', 'limit': 5},
        )
        response.raise_for_status()
        entities = response.json().get('entities') or []
        if not entities:
            raise CompanyNotFound('Crunchbase has no organization matching {!r}'.format(name))
        wanted = clean_name(name).lower()
        for entity in entities:
            identifier = entity.get('identifier') or {}
            if clean_name(identifier.get('value')).lower() == wanted:
                return identifier.get('permalink')
        return (entities[0].get('identifier') or {}).get('permalink')

    # -- web --------------------------------------------------------------

    def _fetch_web(self, company, max_rounds):
        from ..infra.browser import browser, looks_blocked

        slug = permalink(company)
        url = '{}/organization/{}'.format(WEB, slug)
        payloads, texts = [], []

        def capture(response):
            if '/v4/data/' not in response.url and '/v4/entities/' not in response.url:
                return
            with suppress(Exception):
                payloads.append(response.json())

        try:
            with browser(
                self.profile_dir, headless=self.headless, timeout=int(self.timeout * 1000)
            ) as page:
                page.on('response', capture)
                for target in (url, url + '/company_financials'):
                    response = page.goto(target, wait_until='domcontentloaded')
                    if response is not None and response.status == 404:
                        raise CompanyNotFound('no Crunchbase organization at {}'.format(target))
                    self._settle(page, looks_blocked)
                    payloads.extend(embedded_json(page.content()))
                    with suppress(Exception):
                        texts.append(page.inner_text('body'))
        except FinancialsError:
            raise
        # navigation failures, timeouts and a closed browser all surface as plain
        # playwright errors, and each should read as this source failing
        except Exception as error:
            raise FinancialsError(
                'could not load {}: {}'.format(url, str(error).split('\n')[0][:160])
            ) from error

        financials = self._build(payloads, slug, company, max_rounds, url=url)
        self._from_text('\n'.join(texts), financials)
        if (
            financials.total_funding is None
            and not financials.rounds
            and financials.revenue_range is None
        ):
            raise ParseError(
                'no funding data found on {} - the page layout may have changed, '
                'or it needs a signed in profile (--login)'.format(url)
            )
        return financials

    def _settle(self, page, looks_blocked):
        with suppress(Exception):
            page.wait_for_load_state('networkidle', timeout=int(self.timeout * 1000))
        if self.login:
            self.interaction.pause(
                'A browser window is open. Clear the Cloudflare check and sign in to '
                'Crunchbase if you want to, then come back here.'
                ' Press Enter once the company page is visible.'
            )
            self.login = False  # once is enough; the profile remembers
            return
        if looks_blocked(page):
            raise ChallengeError(
                'Crunchbase served a bot check instead of the company page. Re-run with '
                '--login to clear it once in a visible browser; the saved profile is '
                'reused headlessly afterwards.'
            )

    # -- parsing ----------------------------------------------------------

    def _build(self, payloads, slug, company, max_rounds, url=None):
        financials = CompanyFinancials(
            source='crunchbase',
            company=company,
            company_id=slug,
            url=url or '{}/organization/{}'.format(WEB, slug),
        )
        rounds = {}

        for payload in payloads:
            for node in walk(payload):
                self._read_org(node, slug, financials)
                funding_round = self._read_round(node, slug)
                if funding_round is not None:
                    key = funding_round.key()
                    if key not in rounds or _richer(funding_round, rounds[key]):
                        rounds[key] = funding_round

        financials.rounds = sorted(rounds.values(), key=lambda r: r.date or '', reverse=True)[
            : max(0, max_rounds)
        ]
        return financials

    def _read_org(self, node, slug, financials):
        identifier = node.get('identifier')
        if (
            isinstance(identifier, dict)
            and identifier.get('permalink') == slug
            and identifier.get('entity_def_id', 'organization') == 'organization'
            and identifier.get('value')
        ):
            financials.company = identifier['value']

        # anything carrying org level fields is taken, whatever card it sits in -
        # the page splits them across several, the API returns one properties dict
        if 'funding_total' in node and financials.total_funding is None:
            financials.total_funding = to_money(node['funding_total'])
        if financials.funding_rounds_count is None and isinstance(
            node.get('num_funding_rounds'), int
        ):
            financials.funding_rounds_count = node['num_funding_rounds']
        pairs = (
            ('last_funding_type', 'last_funding_type', humanize),
            ('last_funding_date', 'last_funding_at', text_of),
            ('revenue_range', 'revenue_range', revenue_band),
            ('employees', 'num_employees_enum', employee_band),
            ('ipo_status', 'ipo_status', humanize),
            ('stock_symbol', 'stock_symbol', text_of),
            ('founded', 'founded_on', text_of),
            ('website', 'website', text_of),
            ('website', 'website_url', text_of),
            ('description', 'short_description', text_of),
            ('stage', 'funding_stage', humanize),
        )
        for attribute, key, convert in pairs:
            if getattr(financials, attribute) is None and node.get(key) not in (None, ''):
                value = convert(node[key])
                if value:
                    setattr(financials, attribute, value)

    def _read_round(self, node, slug):
        if 'investment_type' not in node or not ('announced_on' in node or 'money_raised' in node):
            return None
        # the org's own investments in other companies look alike; keep only its raises
        funded = node.get('funded_organization_identifier')
        if isinstance(funded, dict) and funded.get('permalink') not in (None, slug):
            return None

        leads = []
        for lead in node.get('lead_investor_identifiers') or []:
            name = text_of(lead)
            if name:
                leads.append(name)

        count = node.get('num_investors')
        return FundingRound(
            date=text_of(node.get('announced_on')),
            round=humanize(node.get('investment_type')),
            amount=to_money(node.get('money_raised')),
            post_money_valuation=to_money(node.get('post_money_valuation')),
            lead_investors=leads,
            investor_count=count if isinstance(count, int) else None,
        )

    def _from_text(self, text, financials):
        """The rendered page, for when the embedded state had nothing."""
        if financials.total_funding is None:
            financials.total_funding = parse_money(
                labelled(text, 'Total Funding Amount', 'Total Funding') or ''
            )
        if financials.last_funding_type is None:
            financials.last_funding_type = labelled(text, 'Last Funding Type')
        if financials.revenue_range is None:
            financials.revenue_range = labelled(text, 'Estimated Revenue Range', 'Annual Revenue')
        if financials.employees is None:
            financials.employees = labelled(text, 'Number of Employees')
        if financials.funding_rounds_count is None:
            count = labelled(text, 'Number of Funding Rounds')
            if count and count.replace(',', '').isdigit():
                financials.funding_rounds_count = int(count.replace(',', ''))


def _richer(new, old):
    """Prefer the copy of a round that carries an amount or a valuation."""
    return (new.amount is not None and old.amount is None) or (
        new.post_money_valuation is not None and old.post_money_valuation is None
    )


def _error(response):
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, list) and body:
        body = body[0]
    if isinstance(body, dict):
        return first(body, 'message', 'error', 'code') or str(body)[:200]
    return str(body)[:200]
