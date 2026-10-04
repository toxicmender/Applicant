"""Crunchbase and Tracxn without an API key - their web pages, on a scripted page
(tests/fakes.py) - and the API and model branches tests/test_financials.py
leaves out.
"""

from __future__ import annotations

import json
import unittest

import httpx

from applicant.errors import AuthFailed, Blocked, NotFound, SourceError, Unparseable
from applicant.financials import CrunchbaseClient, TracxnClient
from applicant.financials.crunchbase import employee_band, revenue_band
from applicant.financials.models import CompanyFinancials, FundingRound, Money, compact
from applicant.financials.tracxn import to_iso
from applicant.interaction import Scripted
from tests.fakes import FakePage, FakeResponse, Visit, patched_browser
from tests.test_financials import TX_ID, crunchbase, tracxn


def ng_state(document: dict) -> str:
    """Crunchbase's Angular transfer state, escaped the way the page ships it."""
    raw = json.dumps(document).replace('"', '&q;')
    return f'<html><script id="ng-state" type="application/json">{raw}</script></html>'


def next_data(document: dict) -> str:
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(document)}</script>'


CB_PAGE = {
    'cards': {
        'identifier': {'permalink': 'zomato', 'value': 'Zomato', 'entity_def_id': 'organization'},
        'funding_total': {'value': 2100000000, 'currency': 'USD', 'value_usd': 2100000000},
        'num_funding_rounds': 20,
    }
}


class CrunchbaseWebTest(unittest.TestCase):
    def fetch(self, site, company='zomato', **options):
        page = FakePage(site)
        client = CrunchbaseClient(api_key='', **options)
        with patched_browser(page) as session:
            result = client.fetch(company)
        return result, page, session, client

    def test_the_page_s_embedded_state_and_its_text_both_count(self):
        site = [
            (r'organization/zomato$', Visit(html=ng_state(CB_PAGE), body='Zomato\nFood delivery')),
            (
                r'company_financials$',
                Visit(
                    body='Last Funding Type\nPost-IPO Equity\nEstimated Revenue Range\n$1B to $10B'
                ),
            ),
        ]
        result, page, session, _ = self.fetch(site)
        assert result.total_funding is not None
        self.assertEqual(result.total_funding.amount_usd, 2.1e9)
        self.assertEqual(result.funding_rounds_count, 20)
        self.assertEqual(result.last_funding_type, 'Post-IPO Equity')
        self.assertEqual(result.revenue_range, '$1B to $10B')
        self.assertEqual(len(page.visited), 2)
        self.assertEqual(session.options[0]['profile_dir'], '.cb_profile')

    def test_the_rendered_text_alone_is_enough(self):
        text = (
            'Total Funding Amount\n$2.1B\nNumber of Employees\n5001-10000\n'
            'Number of Funding Rounds\n1,020\nLast Funding Type\nSeries J'
        )
        result, *_ = self.fetch([(r'organization/zomato', Visit(body=text))])
        assert result.total_funding is not None
        self.assertEqual(result.total_funding.amount, 2.1e9)
        self.assertEqual(result.employees, '5001-10000')
        self.assertEqual(result.funding_rounds_count, 1020)

    def test_api_calls_the_page_makes_are_read_and_others_ignored(self):
        heard = [
            FakeResponse(url='https://www.crunchbase.com/v4/data/entities/x', payload=CB_PAGE),
            FakeResponse(
                url='https://www.crunchbase.com/v4/data/broken', payload=ValueError('html')
            ),
            FakeResponse(
                url='https://www.crunchbase.com/assets/app.js', payload={'funding_total': 1}
            ),
        ]
        result, *_ = self.fetch([(r'organization/zomato', Visit(responses=heard))])
        assert result.total_funding is not None
        self.assertEqual(result.total_funding.amount_usd, 2.1e9)

    def test_no_such_organization(self):
        with self.assertRaises(NotFound):
            self.fetch([(r'organization/zomato$', Visit(status=404))])

    def test_a_bot_check_is_blocked(self):
        with self.assertRaisesRegex(Blocked, '--login'):
            self.fetch([(r'organization/zomato', Visit(html=ng_state(CB_PAGE), blocked=True))])

    def test_login_pauses_once_for_a_person_then_runs_headless_next_time(self):
        said = Scripted()
        # the check the person clears during the pause is gone by the next page
        site = [
            (r'organization/zomato$', Visit(html=ng_state(CB_PAGE), blocked=True)),
            (r'company_financials$', Visit(html=ng_state(CB_PAGE))),
        ]
        _, _, _, client = self.fetch(site, login=True, interaction=said)
        self.assertEqual(len(said.said), 1, 'one pause for two pages')
        self.assertFalse(client.login)

    def test_a_browser_failure_is_this_source_failing_in_one_line(self):
        crashed = Visit(raises=RuntimeError('Timeout 30000ms exceeded.\n=== logs ===\nnavigating'))
        with self.assertRaisesRegex(SourceError, r'could not load .*Timeout 30000ms exceeded\.$'):
            self.fetch([(r'organization/zomato', crashed)])

    def test_a_page_with_nothing_on_it_is_unparseable(self):
        with self.assertRaisesRegex(Unparseable, 'layout may have changed'):
            self.fetch([(r'organization/zomato', Visit(body='Sign in to see more'))])

    def test_a_url_is_reduced_to_its_permalink(self):
        _, page, *_ = self.fetch(
            [(r'organization/zomato', Visit(html=ng_state(CB_PAGE)))],
            company='https://www.crunchbase.com/organization/Zomato/company_financials',
        )
        self.assertEqual(page.visited[0], 'https://www.crunchbase.com/organization/zomato')


class CrunchbaseApiEdgesTest(unittest.TestCase):
    def test_refusals_say_which(self):
        for status, body, pattern in (
            (401, {}, 'rejected the API key'),
            (400, {'error': 'unknown card'}, 'unknown card'),
            (403, 'plain text refusal', 'plain text refusal'),
            (403, [], r'\[\]'),
            (403, '"just a string"', 'just a string'),
        ):
            with self.subTest(status=status, body=body):
                content = body if isinstance(body, str) else json.dumps(body)
                client = crunchbase(lambda request, s=status, c=content: httpx.Response(s, text=c))
                with self.assertRaisesRegex(AuthFailed, pattern):
                    client.fetch('zomato')

    def test_a_server_error_is_reported_by_status(self):
        client = crunchbase(lambda request: httpx.Response(500))
        with self.assertRaisesRegex(SourceError, 'answered HTTP 500'):
            client.fetch('zomato')

    def test_a_body_that_is_not_json_is_unparseable(self):
        client = crunchbase(lambda request: httpx.Response(200, text='<html>'))
        with self.assertRaises(Unparseable):
            client.fetch('zomato')

    def test_with_no_exact_name_the_first_suggestion_is_taken(self):
        def handler(request):
            if request.url.path.endswith('/autocompletes'):
                return httpx.Response(
                    200, json={'entities': [{'identifier': {'permalink': 'zomato-ltd'}}]}
                )
            if request.url.path.endswith('/zomato-ltd'):
                return httpx.Response(
                    200, json={'properties': {'identifier': {'permalink': 'zomato-ltd'}}}
                )
            return httpx.Response(404)

        with self.assertLogs('applicant.financials.crunchbase', 'INFO'):
            self.assertEqual(crunchbase(handler).fetch('Zomato').company_id, 'zomato-ltd')

    def test_the_connection_pool_is_made_once_and_closed(self):
        with crunchbase(lambda request: httpx.Response(404)) as client:
            self.assertIs(client.client, client.client)
        self.assertIsNone(client._http)

    def test_bands(self):
        self.assertIsNone(employee_band(None))
        self.assertEqual(employee_band('about 50'), 'about 50', 'an unknown band is kept as is')
        self.assertEqual(employee_band('c_10001_max'), '10001+')
        self.assertIsNone(revenue_band(7))
        self.assertEqual(revenue_band('r_99'), 'r_99')


PROFILE = f'https://tracxn.com/d/companies/zomato/__{TX_ID}'


class TracxnWebTest(unittest.TestCase):
    def fetch(self, site, company=PROFILE, max_rounds=20, **options):
        page = FakePage(site)
        client = TracxnClient(api_key='', **options)
        with patched_browser(page) as session:
            result = client.fetch(company, max_rounds=max_rounds)
        return result, page, session, client

    def record(self) -> dict:
        return {
            'props': {
                'company': {
                    'id': TX_ID,
                    'name': 'Zomato',
                    'totalMoneyRaised': {'amount': 2.1e9, 'currency': 'USD'},
                }
            }
        }

    def rounds(self, count: int) -> FakeResponse:
        return FakeResponse(
            url='https://tracxn.com/api/funding',
            payload={
                'rounds': [
                    {
                        'fundingRoundCategory': f'Series {chr(65 + n)}',
                        'fundingDate': f'01/0{n + 1}/2020',
                    }
                    for n in range(count)
                ]
            },
        )

    def test_the_profile_s_state_its_api_calls_and_its_text(self):
        body = 'Latest Valuation\n$8B\nAnnual Revenue\n$600M\nEmployee Count\n5000\nLatest Funding Round\nSeries J, Feb 2021'
        site = [
            (
                r'__\w+$',
                Visit(html=next_data(self.record()), responses=[self.rounds(2)], body=body),
            ),
            (r'funding-and-investors$', Visit(status=404)),  # a missing tab is skipped
        ]
        result, page, session, _ = self.fetch(site)
        self.assertEqual(result.company, 'Zomato')
        self.assertEqual(len(result.rounds), 2)
        self.assertEqual(result.funding_rounds_count, 2)
        assert result.valuation is not None and result.revenue is not None
        self.assertEqual((result.valuation.amount, result.revenue.amount), (8e9, 6e8))
        self.assertEqual(result.employees, '5000')
        self.assertEqual(session.options[0]['profile_dir'], '.tx_profile')
        self.assertEqual(len(page.visited), 2)

    def test_rounds_beyond_the_limit_are_cut_and_not_counted(self):
        site = [(r'__\w+$', Visit(html=next_data(self.record()), responses=[self.rounds(3)]))]
        result, *_ = self.fetch(site, max_rounds=2)
        self.assertEqual(len(result.rounds), 2)
        self.assertIsNone(result.funding_rounds_count)

    def test_other_responses_are_not_read(self):
        heard = [
            FakeResponse(
                url='https://cdn.example/api/rounds',
                payload={'rounds': [{'roundName': 'Seed', 'date': '2020-01-01'}]},
            ),
            FakeResponse(url='https://tracxn.com/api/broken', payload=ValueError('html')),
        ]
        site = [(r'__\w+$', Visit(html=next_data(self.record()), responses=heard))]
        result, *_ = self.fetch(site)
        self.assertEqual(result.rounds, [])

    def test_the_text_alone_is_enough(self):
        body = 'Total Funding\n$250M\nCompany Stage\nSeries C\nFounded Year\n2015\nLatest Round\nSeries C'
        result, *_ = self.fetch([(r'__\w+$', Visit(body=body))])
        assert result.total_funding is not None
        self.assertEqual(result.total_funding.amount, 2.5e8)
        self.assertEqual(
            (result.stage, result.founded, result.last_funding_type),
            ('Series C', '2015', 'Series C'),
        )
        self.assertEqual(result.company, 'Zomato', 'named from the url when the page does not say')

    def test_no_such_profile(self):
        with self.assertRaises(NotFound):
            self.fetch([(r'__\w+$', Visit(status=404))])

    def test_a_bot_check_is_blocked(self):
        with self.assertRaises(Blocked):
            self.fetch([(r'__\w+$', Visit(body='Total Funding\n$1M', blocked=True))])

    def test_login_pauses_once(self):
        said = Scripted()
        _, _, _, client = self.fetch(
            [(r'.', Visit(body='Total Funding\n$1M'))], login=True, interaction=said
        )
        self.assertEqual(len(said.said), 1)
        self.assertFalse(client.login)

    def test_a_browser_failure_is_this_source_failing(self):
        with self.assertRaisesRegex(SourceError, 'could not load'):
            self.fetch([(r'__\w+$', Visit(raises=RuntimeError('net::ERR_ABORTED\nmore')))])

    def test_nothing_on_the_page_is_unparseable(self):
        with self.assertRaises(Unparseable):
            self.fetch([(r'.', Visit(body='Sign up to see funding'))])

    def test_only_a_profile_url_can_be_read_without_a_key(self):
        with self.assertRaisesRegex(SourceError, 'not a Tracxn profile url'):
            TracxnClient(api_key='')._fetch_web('https://tracxn.com/elsewhere', 5)


class TracxnApiEdgesTest(unittest.TestCase):
    def test_refusals(self):
        for status, body, error, pattern in (
            (401, {}, AuthFailed, 'rejected the API token'),
            (403, {'message': 'plan does not cover'}, AuthFailed, 'plan does not cover'),
            (403, 'gateway says no', AuthFailed, 'gateway says no'),
            (400, ['bad filter'], SourceError, 'bad filter'),
            (500, {}, SourceError, 'answered HTTP 500'),
            (200, '<html>', Unparseable, 'other than JSON'),
        ):
            with self.subTest(status=status):
                content = body if isinstance(body, str) else json.dumps(body)
                client = tracxn(lambda request, s=status, c=content: httpx.Response(s, text=c))
                with self.assertRaisesRegex(error, pattern):
                    client.fetch(TX_ID)

    def test_an_unreachable_api(self):
        def offline(request):
            raise httpx.ConnectError('down', request=request)

        client = tracxn(offline)
        client.retries = 1
        with self.assertRaisesRegex(SourceError, 'could not reach Tracxn'):
            client.fetch(TX_ID)

    def respond(self, routes: dict):
        def handler(request):
            for suffix, (status, body) in routes.items():
                if request.url.path.endswith(suffix):
                    return httpx.Response(status, json=body)
            return httpx.Response(200, json={'result': []})

        return handler

    def test_rounds_refused_by_the_plan_are_a_note(self):
        client = tracxn(self.respond({'/fundingrounds': (403, {'message': 'not in plan'})}))
        with self.assertLogs('applicant.financials.tracxn', 'WARNING'):
            result = client.fetch(TX_ID)
        self.assertTrue(any(note.startswith('funding rounds:') for note in result.notes))

    def test_a_profile_url_is_looked_up_by_its_name(self):
        seen = []

        def handler(request):
            seen.append(json.loads(request.content))
            if request.url.path.endswith('/companies/search'):
                return httpx.Response(
                    200, json={'result': [{'id': TX_ID, 'companyName': 'Other Co'}]}
                )
            return httpx.Response(200, json={'result': []})

        result = tracxn(handler).fetch(PROFILE)
        self.assertEqual(seen[0]['filter'], {'companyName': 'zomato'})
        self.assertEqual(result.company_id, TX_ID, 'no exact name: the first candidate')

    def test_no_candidate_is_not_found(self):
        with self.assertRaises(NotFound):
            tracxn(self.respond({})).fetch('Nobody')

    def test_a_record_is_found_by_its_fields_when_its_id_is_not_given(self):
        client = TracxnClient(api_key='t')
        record = client._record({'a': {'name': 'Zomato', 'domain': 'zomato.com'}}, None)
        self.assertEqual(record, {'name': 'Zomato', 'domain': 'zomato.com'})
        self.assertIsNone(client._record({'a': {'name': 'Zomato'}}, None))

    def test_round_shapes(self):
        client = TracxnClient(api_key='t')
        with_investors = client._read_round(
            {
                'roundName': 'Seed',
                'date': '2019-05-01',
                'leadInvestor': {'name': 'Y Combinator'},
                'investors': [1, 2, 3],
            }
        )
        assert with_investors is not None
        self.assertEqual(with_investors.lead_investors, ['Y Combinator'])
        self.assertEqual(with_investors.investor_count, 3)
        self.assertIsNone(
            client._read_round(
                {'stage': 'Seed', 'roundName': 'Seed', 'date': '2020', 'totalFunding': 1}
            )
        )
        self.assertIsNone(client._read_round({'roundName': 'Seed'}), 'no date and no amount')

    def test_a_series_point_without_a_year_or_value_is_skipped(self):
        client = TracxnClient(api_key='t')
        self.assertEqual(
            client._latest_point({'p': [{'year': {'x': 1}, 'value': 1}, {'year': 2020}]}),
            (None, None),
        )

    def test_the_copy_with_an_amount_wins(self):
        from applicant.financials.tracxn import _dedupe

        bare = FundingRound(date='2020-01-01', round='Seed')
        priced = FundingRound(
            date='2020-01-01', round='Seed', amount=Money(amount=1e6, currency='USD')
        )
        self.assertEqual(_dedupe([bare, priced]), [priced])

    def test_dates(self):
        for value, expected in (
            ({'year': 2021, 'month': 3, 'day': 5}, '2021-03-05'),
            ({'year': 2021}, '2021'),
            ({'date': '17/02/2021'}, '2021-02-17'),
            (1613520000000, '2021-02-17'),
            (1613520000, '2021-02-17'),
            (10**30, None),
            ('2021-02-17T10:00:00Z', '2021-02-17'),
            ('Feb 17, 2021', '2021-02-17'),
            ('March 2021', '2021-03-01'),
            ('sometime', 'sometime'),
            ('  ', None),
            (None, None),
        ):
            with self.subTest(value=value):
                self.assertEqual(to_iso(value), expected)

    def test_the_connection_pool_is_closed_on_exit(self):
        with tracxn(self.respond({})) as client:
            self.assertIs(client.client, client.client)
        self.assertIsNone(client._http)


class ModelEdgesTest(unittest.TestCase):
    def test_money(self):
        usd = Money(amount=1e6, currency='USD', amount_usd=1e6)
        self.assertFalse(usd.same_as(None))
        self.assertTrue(usd.same_as(Money(amount=8e7, currency='INR', amount_usd=1e6)))
        self.assertFalse(usd.same_as(Money(amount=1e6, currency='EUR')))
        self.assertEqual(str(Money(text='Undisclosed')), 'Undisclosed')
        self.assertEqual(str(Money()), 'unknown')
        self.assertEqual(compact(950, 'USD'), '$950')
        self.assertEqual(compact(2e6), '2M')
        self.assertEqual(compact(2e6, 'CHF'), 'CHF 2M')

    def test_summary(self):
        self.assertEqual(
            CompanyFinancials(source='x', company='y').summary(), 'no financial data found'
        )
        full = CompanyFinancials(
            source='x',
            company='y',
            total_funding=Money(amount=2.1e9, currency='USD'),
            funding_rounds_count=1,
            last_funding_type='Series J',
            valuation=Money(amount=8e9, currency='USD'),
            valuation_date='2021',
            revenue=Money(amount=6e8, currency='USD'),
            revenue_year='2024',
            stage='Public',
        )
        self.assertEqual(
            full.summary(),
            'raised $2.1B over 1 round, last Series J, valued at $8B (2021), revenue $600M (2024), Public',
        )
        ranged = CompanyFinancials(source='x', company='y', revenue_range='$1B to $10B')
        self.assertEqual(ranged.summary(), 'revenue $1B to $10B')


if __name__ == '__main__':
    unittest.main()


class ParsingEdgesTest(unittest.TestCase):
    def test_money_from_text(self):
        from applicant.financials.parsing import parse_money

        arr = parse_money('ARR 5M')
        assert arr is not None
        self.assertEqual((arr.amount, arr.currency), (5e6, None), 'ARR is a word, not a currency')
        self.assertIsNone(parse_money('founded 2015'), 'a bare number is a count or a year')
        self.assertIsNone(parse_money(None))

    def test_money_from_shapes(self):
        from applicant.financials.parsing import to_money

        cases = (
            ({'amount': 5e6, 'currencyCode': 'eur'}, (5e6, 'EUR', None)),
            ({'value': 5e6, 'value_usd': 5e6}, (5e6, 'USD', 5e6)),
            ({'amount': 5e6, 'amountInUSD': 6e6}, (5e6, 'USD', 6e6)),
            ({'amount': 5e6}, (5e6, None, None)),
            ({'formatted': '$2.1B'}, (2.1e9, 'USD', 2.1e9)),
            (
                {'label': 'nothing here', 'inner': {'amount': 3.0, 'currency': 'GBP'}},
                (3.0, 'GBP', None),
            ),
            ({'INR': {'amount': 100.0}}, (100.0, 'INR', None)),
            ([None, '₹1,200 Cr'], (1.2e10, 'INR', None)),
        )
        for value, (amount, currency, usd) in cases:
            with self.subTest(value=value):
                money = to_money(value)
                assert money is not None
                self.assertEqual(
                    (money.amount, money.currency, money.amount_usd), (amount, currency, usd)
                )
        for nothing in (True, object(), [None], {'note': 'x'}):
            with self.subTest(nothing=nothing):
                self.assertIsNone(to_money(nothing))

    def test_page_helpers(self):
        from applicant.financials.parsing import embedded_json, humanize, labelled

        broken = '<script id="__NEXT_DATA__" type="application/json">{not json</script>'
        self.assertEqual(embedded_json(broken), [])
        self.assertEqual(
            labelled('Total Funding:\n$2.1B', 'Total Funding'), '$2.1B', 'on the next line'
        )
        self.assertIsNone(
            labelled('Total Funding:   ', 'Total Funding'), 'a label with nothing after it'
        )
        self.assertEqual(humanize('Series A'), 'Series A', 'already human')
        self.assertEqual(humanize('post_ipo_equity'), 'Post IPO Equity')
