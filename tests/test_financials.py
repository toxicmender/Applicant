"""Offline tests for applicant.financials: parsing, both API clients and the tracker."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

import httpx

from applicant.errors import AuthFailed, NotFound, QuotaExhausted, SourceError
from applicant.financials import CrunchbaseClient, FinancialsTracker, TracxnClient, parse_money
from applicant.financials.crunchbase import employee_band, permalink
from applicant.financials.models import CompanyFinancials, FundingRound, Money
from applicant.financials.parsing import clean_name, embedded_json, humanize, labelled, to_money
from applicant.financials.tracxn import to_iso


class ParseMoney(unittest.TestCase):
    def check(self, text, amount, currency):
        money = parse_money(text)
        assert money is not None, text
        self.assertAlmostEqual(money.amount, amount, places=0)
        self.assertEqual(money.currency, currency)

    def test_forms(self):
        self.check('$2.1B', 2.1e9, 'USD')
        self.check('US$ 450M', 450e6, 'USD')
        self.check('₹1,200 Cr', 1.2e10, 'INR')
        self.check('12.5 Crore', 1.25e8, 'INR')
        self.check('€30 million', 30e6, 'EUR')
        self.check('INR 3 Lakh', 3e5, 'INR')
        self.check('500K USD', 5e5, 'USD')
        self.check('£1.5bn', 1.5e9, 'GBP')

    def test_unknowns_are_none_not_zero(self):
        for text in ('Undisclosed', '—', '-', 'N/A', '', None, 'Founded 2008', '120 employees'):
            self.assertIsNone(parse_money(text), text)

    def test_usd_is_carried(self):
        dollars, rupees = parse_money('$10M'), parse_money('₹10 Cr')
        assert dollars is not None and rupees is not None
        self.assertEqual(dollars.amount_usd, 10e6)
        self.assertIsNone(rupees.amount_usd)

    def test_structured(self):
        money = to_money({'value': 250000000, 'currency': 'USD', 'value_usd': 250000000})
        self.assertEqual((money.amount, money.currency, money.amount_usd), (250e6, 'USD', 250e6))
        money = to_money({'totalAmount': {'amount': 5000000, 'currency': 'EUR'}})
        self.assertEqual((money.amount, money.currency), (5e6, 'EUR'))
        self.assertEqual(to_money(1500).currency, 'USD')
        self.assertIsNone(to_money({'foo': 'bar'}))

    def test_compact_display(self):
        self.assertEqual(str(Money(amount=2.1e9, currency='USD')), '$2.1B')
        self.assertEqual(str(Money(amount=1.2e10, currency='INR')), '₹12B')
        self.assertEqual(str(Money(amount=3e6, currency='SGD')), 'SGD 3M')


class Helpers(unittest.TestCase):
    def test_permalink(self):
        self.assertEqual(
            permalink('https://www.crunchbase.com/organization/zomato/company_financials'), 'zomato'
        )
        self.assertEqual(permalink('Tata Consultancy Services'), 'tata-consultancy-services')

    def test_bands(self):
        self.assertEqual(employee_band('c_01001_05000'), '1001-5000')
        self.assertEqual(employee_band('c_10001_max'), '10001+')

    def test_clean_name(self):
        self.assertEqual(clean_name('Zomato Pvt. Ltd.'), 'Zomato')
        self.assertEqual(clean_name('Infosys Limited'), 'Infosys')
        self.assertEqual(clean_name('Stripe, Inc.'), 'Stripe')
        self.assertEqual(clean_name('Co'), 'Co')

    def test_humanize(self):
        self.assertEqual(humanize('series_a'), 'Series A')
        self.assertEqual(humanize('post_ipo_equity'), 'Post IPO Equity')
        self.assertEqual(humanize('Series B'), 'Series B')

    def test_dates(self):
        self.assertEqual(to_iso('12/07/2021'), '2021-07-12')
        self.assertEqual(to_iso(1626048000000), '2021-07-12')
        self.assertEqual(to_iso('Jul 12, 2021'), '2021-07-12')

    def test_labelled(self):
        text = 'Overview\nTotal Funding\n$2.1B\nStage\nPublic\n'
        self.assertEqual(labelled(text, 'Total Funding'), '$2.1B')
        self.assertEqual(labelled(text, 'Stage'), 'Public')

    def test_angular_state(self):
        html = (
            '<script id="ng-state" type="application/json">'
            '{&q;funding_total&q;:{&q;value&q;:5}}</script>'
        )
        self.assertEqual(embedded_json(html), [{'funding_total': {'value': 5}}])


# the shape card_ids=fields,raised_funding_rounds comes back in: the properties
# sit in the `fields` card rather than under `properties`
CB_ORG = {
    'properties': {'identifier': {'permalink': 'zomato', 'value': 'Zomato'}},
    'cards': {
        'fields': {
            'identifier': {
                'permalink': 'zomato',
                'value': 'Zomato',
                'entity_def_id': 'organization',
            },
            'funding_total': {'value': 2100000000, 'currency': 'USD', 'value_usd': 2100000000},
            'num_funding_rounds': 20,
            'last_funding_type': 'post_ipo_equity',
            'last_funding_at': '2024-11-28',
            'revenue_range': 'r_01000000',
            'num_employees_enum': 'c_05001_10000',
            'ipo_status': 'public',
            'stock_symbol': {'value': 'ZOMATO'},
            'founded_on': {'value': '2008-07-10', 'precision': 'day'},
        },
        'raised_funding_rounds': [
            {
                'identifier': {'value': 'Series J - Zomato'},
                'announced_on': '2021-02-17',
                'investment_type': 'series_j',
                'money_raised': {'value': 250000000, 'currency': 'USD', 'value_usd': 250000000},
                'post_money_valuation': {
                    'value': 5400000000,
                    'currency': 'USD',
                    'value_usd': 5400000000,
                },
                'lead_investor_identifiers': [{'value': 'Tiger Global Management'}],
                'num_investors': 5,
                'funded_organization_identifier': {'permalink': 'zomato'},
            },
            {
                'announced_on': '2013-11-12',
                'investment_type': 'series_e',
                'money_raised': {'value': 37000000, 'currency': 'USD', 'value_usd': 37000000},
                'funded_organization_identifier': {'permalink': 'zomato'},
            },
            # something Zomato invested in, not a round it raised
            {
                'announced_on': '2022-01-01',
                'investment_type': 'series_b',
                'money_raised': {'value': 1, 'currency': 'USD'},
                'funded_organization_identifier': {'permalink': 'shiprocket'},
            },
        ],
    },
}


def crunchbase(handler):
    return CrunchbaseClient(
        api_key='k', client=httpx.Client(transport=httpx.MockTransport(handler)), delay=0
    )


class Crunchbase(unittest.TestCase):
    def test_entity_lookup(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json=CB_ORG)

        result = crunchbase(handler).fetch('zomato')
        self.assertEqual(seen[0].headers['X-cb-user-key'], 'k')
        self.assertEqual(seen[0].url.params['card_ids'], 'fields,raised_funding_rounds')
        self.assertNotIn('field_ids', seen[0].url.params)
        self.assertEqual(result.company, 'Zomato')
        assert result.total_funding is not None and result.valuation is not None
        self.assertEqual(result.total_funding.amount_usd, 2.1e9)
        self.assertEqual(result.funding_rounds_count, 20)
        self.assertEqual(result.last_funding_type, 'Post IPO Equity')
        self.assertEqual(result.revenue_range, '$1B to $10B')
        self.assertEqual(result.employees, '5001-10000')
        self.assertEqual(result.stock_symbol, 'ZOMATO')
        self.assertEqual(result.founded, '2008-07-10')
        self.assertEqual([r.round for r in result.rounds], ['Series J', 'Series E'])
        self.assertEqual(result.rounds[0].lead_investors, ['Tiger Global Management'])
        # no valuation on the org; it comes from the latest round that states one
        self.assertEqual(result.valuation.amount, 5.4e9)
        self.assertEqual(result.valuation_date, '2021-02-17')

    def test_name_resolved_by_autocomplete(self):
        def handler(request):
            path = request.url.path
            if path.endswith('/autocompletes'):
                self.assertEqual(request.url.params['query'], 'Eternal')
                return httpx.Response(
                    200,
                    json={
                        'entities': [
                            {
                                'identifier': {
                                    'permalink': 'eternal-foods',
                                    'value': 'Eternal Foods',
                                }
                            },
                            {'identifier': {'permalink': 'zomato', 'value': 'Eternal Limited'}},
                        ]
                    },
                )
            if path.endswith('/organizations/zomato'):
                return httpx.Response(200, json=CB_ORG)
            return httpx.Response(404, json={'message': 'not found'})

        # no 'eternal' permalink, so the name goes through autocomplete and the
        # legal suffixes on both sides are ignored when picking the match
        self.assertEqual(crunchbase(handler).fetch('Eternal Pvt. Ltd.').company_id, 'zomato')

    def test_basic_plan_is_explained(self):
        client = crunchbase(lambda request: httpx.Response(403, json=[{'message': 'nope'}]))
        with self.assertRaisesRegex(AuthFailed, 'Basic'):
            client.fetch('zomato')

    def test_single_attempt_reports_transport_error(self):
        def handler(request):
            raise httpx.ConnectError('offline', request=request)

        client = crunchbase(handler)
        client.retries = 0  # still one attempt, and its failure must be reported
        with self.assertRaisesRegex(SourceError, 'could not reach Crunchbase'):
            client.fetch('zomato')

    def test_not_found(self):
        def handler(request):
            if request.url.path.endswith('/autocompletes'):
                return httpx.Response(200, json={'entities': []})
            return httpx.Response(404)

        with self.assertRaises(NotFound):
            crunchbase(handler).fetch('no such company')


TX_ID = '5c1c697b8f088f5b6f55226c'


def tracxn(handler):
    return TracxnClient(
        api_key='t', client=httpx.Client(transport=httpx.MockTransport(handler)), delay=0
    )


class Tracxn(unittest.TestCase):
    def handler(self, calls):
        def respond(request):
            body = json.loads(request.content)
            calls.append((request.url.path, body, request.headers.get('accessToken')))
            path = request.url.path
            if path == '/api/2.2/companies/search':
                return httpx.Response(
                    200,
                    json={
                        'result': [
                            {'id': '000000000000000000000001', 'name': 'Zomato Hyperpure'},
                            {'id': TX_ID, 'name': 'Zomato'},
                        ]
                    },
                )
            if path == '/api/3.0/companies':
                return httpx.Response(
                    200,
                    json={
                        'result': [
                            {
                                'id': TX_ID,
                                'name': 'Zomato',
                                'domain': 'zomato.com',
                                'foundedYear': 2008,
                                'companyStage': 'Public',
                                'totalMoneyRaised': {
                                    'totalAmount': {'amount': 2100000000, 'currency': 'USD'}
                                },
                            }
                        ]
                    },
                )
            if path == '/api/3.0/fundingrounds':
                return httpx.Response(
                    200,
                    json={
                        'result': [
                            {
                                'fundingRoundCategory': 'Series J',
                                'fundingDate': '17/02/2021',
                                'fundingAmount': {'amount': 250000000, 'currency': 'USD'},
                                'leadInvestors': [{'name': 'Tiger Global'}],
                            }
                        ]
                    },
                )
            if path == '/api/3.0/timeseries/valuation':
                return httpx.Response(
                    200,
                    json={
                        'result': [
                            {'year': 2023, 'value': 8000000000, 'currency': 'USD'},
                            {'year': 2024, 'value': 20000000000, 'currency': 'USD'},
                        ]
                    },
                )
            if path == '/api/3.0/timeseries/revenue':
                return httpx.Response(
                    403, json={'errorCode': 403000000, 'message': 'Not in your plan'}
                )
            return httpx.Response(404)

        return respond

    def test_api_flow(self):
        calls = []
        result = tracxn(self.handler(calls)).fetch('Zomato')

        self.assertTrue(all(token == 't' for _, _, token in calls))
        self.assertEqual(calls[0][1], {'filter': {'companyName': 'Zomato'}, 'size': 5})
        self.assertEqual(calls[2][1]['filter'], {'companiesId': [TX_ID]})
        self.assertEqual(result.company_id, TX_ID)
        assert result.total_funding is not None and result.valuation is not None
        self.assertEqual(result.total_funding.amount, 2.1e9)
        self.assertEqual(result.stage, 'Public')
        self.assertEqual(result.founded, '2008')
        self.assertEqual(result.rounds[0].date, '2021-02-17')
        self.assertEqual(result.rounds[0].lead_investors, ['Tiger Global'])
        self.assertEqual(result.last_funding_type, 'Series J')
        self.assertEqual((result.valuation.amount, result.valuation_date), (2e10, '2024'))
        self.assertIsNone(result.revenue)
        self.assertTrue(any(note.startswith('revenue:') for note in result.notes))

    def test_rounds_are_counted_only_when_the_api_ran_out_of_them(self):
        """A short page ends the list; stopping at max_rounds may not have."""
        whole = tracxn(self.handler([])).fetch('Zomato', max_rounds=20)
        self.assertEqual(whole.funding_rounds_count, 1)
        cut = tracxn(self.handler([])).fetch('Zomato', max_rounds=1)
        self.assertEqual(len(cut.rounds), 1)
        self.assertIsNone(cut.funding_rounds_count)

    def test_domain_lookup(self):
        calls = []
        tracxn(self.handler(calls)).fetch('https://www.zomato.com/')
        self.assertEqual(
            calls[0][:2], ('/api/3.0/companies', {'filter': {'domain': ['zomato.com']}, 'size': 1})
        )

    def test_out_of_credits_is_not_retried(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(403, json={'errorCode': 900, 'message': 'API out of credits'})

        # its own kind, not a refusal that happens to quote the message: the CLI
        # tells a person to renew credits rather than to check the token
        with self.assertRaisesRegex(QuotaExhausted, 'retrying cannot help'):
            tracxn(handler).fetch(TX_ID)
        self.assertEqual(len(calls), 1)

    def test_name_without_token_is_explained(self):
        with self.assertRaisesRegex(SourceError, 'profile url'):
            TracxnClient(api_key='').fetch('Zomato')


class RoundCount(unittest.TestCase):
    """A round count read off a list is only a count when the list is whole."""

    ROUNDS = tuple(
        {'investment_type': 'series_a', 'announced_on': '2020-01-0{}'.format(n)} for n in (1, 2, 3)
    )

    def build(self, max_rounds):
        financials = CrunchbaseClient()._build(
            [{'rounds': list(self.ROUNDS)}], 'acme', 'Acme', max_rounds
        )
        return financials.fill_from_rounds()

    def test_a_whole_list_is_counted(self):
        self.assertEqual(self.build(max_rounds=5).funding_rounds_count, 3)

    def test_a_list_cut_at_max_rounds_is_not(self):
        """-n 2 over three rounds is not 'raised over 2 rounds'."""
        financials = self.build(max_rounds=2)
        self.assertEqual(len(financials.rounds), 2)
        self.assertIsNone(financials.funding_rounds_count)
        self.assertNotIn('over 2 round', financials.summary())

    def test_a_stated_count_is_kept_either_way(self):
        financials = CrunchbaseClient()._build(
            [{'rounds': list(self.ROUNDS)}, {'num_funding_rounds': 30}], 'acme', 'Acme', 2
        )
        self.assertEqual(financials.fill_from_rounds().funding_rounds_count, 30)

    def test_the_count_is_not_part_of_the_stored_record(self):
        self.assertNotIn('_rounds_complete', json.dumps(self.build(max_rounds=2).to_dict()))


class Teardown(unittest.TestCase):
    """Whoever makes a client closes it; a pool handed in belongs to the caller."""

    def test_the_api_pool_is_closed_once_opened(self):
        for make in (CrunchbaseClient, TracxnClient):
            with self.subTest(client=make.__name__):
                with make(api_key='k' * 12) as client:
                    pool = client.http.client
                self.assertTrue(pool.is_closed)
                client.close()  # twice is fine

    def test_a_client_that_never_opened_a_pool_closes_quietly(self):
        for make in (CrunchbaseClient, TracxnClient):
            with self.subTest(client=make.__name__):
                make().close()

    def test_a_pool_handed_in_is_left_open(self):
        handed = httpx.Client()
        self.addCleanup(handed.close)
        with CrunchbaseClient(api_key='k' * 12, client=handed) as client:
            client.http  # noqa: B018 - opening it is the point
        self.assertFalse(handed.is_closed)


class Tracker(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix='.json')
        os.close(handle)
        os.remove(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def financials(self, total, rounds, stage='Series J'):
        return CompanyFinancials(
            source='crunchbase',
            company='Zomato',
            company_id='zomato',
            total_funding=Money(amount=total, currency='USD', amount_usd=total),
            stage=stage,
            rounds=rounds,
        )

    def test_changes_and_history(self):
        series_j = FundingRound(
            date='2021-02-17', round='Series J', amount=Money(amount=250e6, currency='USD')
        )
        tracker = FinancialsTracker(self.path)
        self.assertEqual(tracker.record(self.financials(2.1e9, [series_j])), [])

        # nothing moved: no new snapshot
        tracker = FinancialsTracker(self.path)
        self.assertEqual(tracker.record(self.financials(2.1e9, [series_j])), [])
        self.assertEqual(len(tracker.history('crunchbase:zomato')), 1)

        series_k = FundingRound(
            date='2026-09-01',
            round='Series K',
            amount=Money(amount=250e6, currency='USD'),
            lead_investors=['Temasek'],
        )
        changes = FinancialsTracker(self.path).record(self.financials(2.35e9, [series_k, series_j]))
        self.assertEqual(
            changes,
            [
                'total funding: $2.1B -> $2.35B',
                'new round: Series K on 2026-09-01 ($250M; led by Temasek)',
            ],
        )
        tracker = FinancialsTracker(self.path)
        self.assertEqual(len(tracker.history('crunchbase:zomato')), 2)

    def test_missing_field_is_not_a_change(self):
        tracker = FinancialsTracker(self.path)
        tracker.record(self.financials(2.1e9, []))
        partial = CompanyFinancials(source='crunchbase', company='Zomato', company_id='zomato')
        self.assertEqual(tracker.record(partial), [])
        latest = tracker.latest('crunchbase:zomato')
        assert latest is not None and latest.total_funding is not None
        self.assertEqual(latest.total_funding.amount, 2.1e9)
        self.assertEqual(latest.stage, 'Series J')


if __name__ == '__main__':
    unittest.main()
