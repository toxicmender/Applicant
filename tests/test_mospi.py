"""Synthetic PLFS golden fixtures; no live provider dependency in CI."""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest import mock

import httpx
from hypothesis import given
from hypothesis import strategies as st

from applicant.cli import main
from applicant.domain.benchmarks import BenchmarkQuery, BenchmarkResponse
from applicant.domain.mospi import (
    MAX_BYTES,
    PAGE_SIZE,
    parse_metadata,
    parse_observation,
    parse_page,
    payload,
    reference_period,
    validate_query,
    validate_selection,
)
from applicant.errors import ConfigError, SourceError, Unparseable, Unreachable
from applicant.infra.mospi import API_URL, MoSPIProvider
from applicant.infra.store.benchmarks import BenchmarkRepository
from applicant.infra.store.sqlite import Store
from tests.ranking_fixtures import NOW

FIXTURES = Path(__file__).parent / 'fixtures' / 'benchmarks'


def raw(name):
    return (FIXTURES / f'mospi-{name}.json').read_bytes()


def query(**filters):
    base = BenchmarkQuery.model_validate_json(raw('query'))
    return BenchmarkQuery.model_validate(base.model_dump() | {'filters': base.filters | filters})


def response(value):
    return BenchmarkResponse.capture(API_URL, json.dumps(value).encode())


def metadata():
    return parse_metadata(
        BenchmarkResponse.capture(API_URL, raw('indicators')),
        BenchmarkResponse.capture(API_URL, raw('filters')),
    )


def row(**updates):
    return json.loads(raw('values'))['data'][0] | updates


class MoSPIDomainTest(unittest.TestCase):
    def observation(self, document=None, **updates):
        return parse_observation(
            row(**updates) if document is None else document,
            response({}),
            query(),
            metadata(),
            as_of=NOW.date(),
        )

    def test_golden_quarters_decimal_and_cohorts(self):
        document = BenchmarkResponse.capture(API_URL, raw('values'))
        rows, totals = parse_page(document, page=1)
        self.assertEqual(totals, (4, 1))
        values = [
            parse_observation(r, document, query(), metadata(), as_of=NOW.date()) for r in rows
        ]
        self.assertEqual(
            [r.amount for r in values],
            list(map(Decimal, ('20000.125', '21000.25', '22000.5', '23000.75'))),
        )
        self.assertEqual(
            [r.cohort.reference_start for r in values],
            [date(2023, 7, 1), date(2023, 10, 1), date(2024, 1, 1), date(2024, 4, 1)],
        )
        self.assertEqual(values[-1].cohort.reference_end, date(2024, 6, 30))
        self.assertEqual(values[0].cohort.dimensions['year_type_label'], 'Agriculture Year')
        self.assertEqual(values[0].cohort.geography_code, '99')
        self.assertEqual(values[0].cohort.occupation_code, 'all')
        self.assertEqual(values[0].cohort.basis, 'unknown')
        self.assertEqual(values[0].period, 'monthly')
        self.assertEqual(values[0].currency, 'INR')
        self.assertEqual(len({r.observation_id for r in values}), 4)
        validate_selection(query(), metadata())

    def test_calendar_year_and_survey_design_separation(self):
        for year, design in (('2024', 'PLFS-pre-2025-v1'), ('2025', 'PLFS-redesigned-2025-v1')):
            q = query(year=(year,), year_type=('2',))
            r = row(year=year, year_type='Calendar Year', quarter=None)
            value = parse_observation(r, response({}), q, metadata(), as_of=NOW.date())
            self.assertEqual(value.cohort.methodology, design)
            self.assertEqual(value.cohort.reference_start, date(int(year), 1, 1))
            self.assertEqual(value.cohort.reference_end, date(int(year), 12, 31))
        self.assertEqual(
            reference_period('2023-24', '1', None), (date(2023, 7, 1), date(2024, 6, 30))
        )
        self.assertEqual(reference_period('2025', '2', '4'), (date(2025, 1, 1), date(2025, 3, 31)))
        self.assertEqual(reference_period('2025', '2', '2'), (date(2025, 7, 1), date(2025, 9, 30)))
        for r, q, as_of in (
            (row(year='2024-25', quarter=None), query(year=('2024-25',)), NOW.date()),
            (row(), query(), date(2023, 8, 1)),
        ):
            with self.assertRaises(ValueError):
                parse_observation(r, response({}), q, metadata(), as_of=as_of)

    def test_suppression_missing_censoring_and_quality_annotations(self):
        for value, status, amount in (
            ('*', 'suppressed', None),
            ('-', 'unavailable', None),
            ('..', 'unavailable', None),
            ('NA', 'unavailable', None),
            (None, 'unavailable', None),
            ('0', 'available', Decimal(0)),
            (12000, 'available', Decimal(12000)),
            ('50000+', 'censored', Decimal(50000)),
        ):
            r = self.observation(value=value, flags=['P'], notes=['Synthetic quality note'])
            self.assertEqual((r.status, r.amount), (status, amount))
            self.assertEqual(r.flags, ('P',))
            self.assertEqual(r.notes[0], 'Synthetic quality note')

    @given(st.integers(min_value=0, max_value=10**9))
    def test_source_decimals_are_exact(self, amount):
        source = response({'statusCode': True, 'value': 'replace'})
        body = source.body.replace(b'"replace"', (str(amount) + '.125').encode())
        parsed = payload(BenchmarkResponse.capture(API_URL, body))['value']
        self.assertIsInstance(parsed, Decimal)
        self.assertEqual(self.observation(value=parsed).amount, Decimal(amount) + Decimal('.125'))

    def test_preflight_rejects_unbounded_or_unsupported_queries(self):
        for changes in (
            {'provider': 'other'},
            {'dataset': 'other'},
            {'filters': {}},
            *(
                {'filters': query().filters | patch}
                for patch in (
                    {'year': ('2023-24', '2022-23')},
                    {'year_type': ('3',)},
                    {'year_type': ('1', '2')},
                    {'year': ('2023',)},
                    {'year': ('2023-25',)},
                    {'year': ('2016-17',)},
                    {'year': ('2099-00',)},
                    {'state': ('01',)},
                    {'extra': ('x',)},
                    {'state': tuple(str(v) for v in range(251))},
                )
            ),
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_query(
                    BenchmarkQuery.model_validate(query().model_dump() | changes), as_of=NOW.date()
                )
        for q in (query(), query(year_type=('2',), year=('2025',)), query(quarter=('2', '3'))):
            self.assertEqual(validate_query(q, as_of=NOW.date()), q)
        with self.assertRaises(ValueError):
            validate_selection(query(state=('98',)), metadata())

    def test_strict_json_and_metadata_integrity(self):
        for body in (
            b'[]',
            b'{"statusCode":false}',
            b'{"statusCode":true,"error":null}',
            b'{"statusCode":true,"x":NaN}',
            b'{"x":1,"x":2}',
            b'\xff',
            b' ' * (MAX_BYTES + 1),
        ):
            with self.subTest(body=body[:100]), self.assertRaises((ValueError, UnicodeError)):
                payload(BenchmarkResponse.capture(API_URL, body))
        indicators, filters = json.loads(raw('indicators')), json.loads(raw('filters'))
        variants = [
            ([], filters),
            (indicators | {'data': []}, filters),
            (indicators | {'data': [None]}, filters),
            (indicators, filters | {'data': {}}),
        ]
        for change in (
            'duplicate_indicator',
            'wrong_indicator',
            'missing_indicator_code',
            'empty',
            'not_list',
            'not_object',
            'blank',
            'bad_code',
            'duplicate',
            'ambiguous',
            'year_type',
            'quarter',
            'bad_year',
        ):
            a, b = copy.deepcopy(indicators), copy.deepcopy(filters)
            if change == 'duplicate_indicator':
                a['data'].append(a['data'][0])
            elif change == 'wrong_indicator':
                a['data'][0]['description'] = 'Different earnings'
            elif change == 'missing_indicator_code':
                a['data'][0].pop('indicator_code')
            elif change == 'empty':
                b['data']['state'] = []
            elif change == 'not_list':
                b['data']['state'] = {}
            elif change == 'not_object':
                b['data']['state'] = [None]
            elif change == 'blank':
                b['data']['state'][0]['description'] = ' '
            elif change == 'bad_code':
                b['data']['state'][0]['state_code'] = True
            elif change == 'duplicate':
                b['data']['state'].append(b['data']['state'][0])
            elif change == 'ambiguous':
                b['data']['state'][1]['description'] = 'All India'
            elif change == 'year_type':
                b['data']['year_type'][1]['description'] = 'Financial Year'
            elif change == 'quarter':
                b['data']['quarter'][0]['quarter_code'] = 1
            else:
                b['data']['year'][0]['year'] = 2025
            variants.append((a, b))
        for a, b in variants:
            with self.subTest(a=a, b=b), self.assertRaises(ValueError):
                parse_metadata(response(a), response(b))
        filters['data']['state'][0]['state_code'] = '99'
        self.assertEqual(parse_metadata(response(indicators), response(filters)), metadata())

    def test_row_policy_and_selection_integrity(self):
        for patch in (
            {'extra_dimension': 'hidden'},
            {'indicator': 'Other'},
            {'frequency': 'Quarterly'},
            {'unit': 'USD'},
            {'month': 'January'},
            {'year': '2024'},
            {'state': 'Unknown'},
            {'state': 123},
            {'state': 'Andhra Pradesh'},
            {'gender': 'male'},
            {'sector': 'urban'},
            {'quarter': 'Q1'},
            {'flags': {}},
            {'notes': [None]},
            {'value': True},
            {'value': '-10'},
            {'value': {}},
            {'value': '1e100'},
            {'value': 'NaN'},
            {'value': 'Infinity'},
            {'value': ' '},
        ):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                self.observation(row(**patch))
        with self.assertRaises(ValueError):
            self.observation([])
        truncated = row()
        truncated.pop('value')
        with self.assertRaises(ValueError):
            self.observation(truncated)
        with self.assertRaises(ValueError):
            parse_observation(
                row(quarter=None), response({}), query(quarter=('2',)), metadata(), as_of=NOW.date()
            )

    def test_pagination_integrity(self):
        base = json.loads(raw('values'))
        for patch in (
            {'data': {}},
            {'meta_data': None},
            *(
                {'meta_data': base['meta_data'] | p}
                for p in (
                    {'page': True},
                    {'page': 2},
                    {'totalRecords': 0},
                    {'totalRecords': 1001},
                    {'totalPages': 0},
                    {'totalPages': 11},
                    {'totalPages': 2},
                    {'recordPerPage': 2},
                    {'totalRecords': 5},
                )
            ),
        ):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                parse_page(response(base | patch), page=1)
        with self.assertRaises(ValueError):
            parse_page(response(base), page=2)


class MoSPIAcquisitionTest(unittest.TestCase):
    def client(self, *, data=None, filters=None, indicators=None, status=200, unreachable=False):
        calls = []

        def handler(request):
            calls.append(request)
            if unreachable:
                raise httpx.ConnectError('verified TLS unavailable', request=request)
            endpoint = request.url.path.split('/')[-1]
            if endpoint == 'getIndicatorListByFrequency':
                content = raw('indicators') if indicators is None else indicators
            elif endpoint == 'getFilterByIndicatorId':
                content = raw('filters') if filters is None else filters
            else:
                content = raw('values') if data is None else data(request)
            return httpx.Response(status, content=content)

        return httpx.Client(transport=httpx.MockTransport(handler)), calls

    def test_discovery_fetch_and_exact_provenance(self):
        client, calls = self.client()
        with client:
            discovery = MoSPIProvider(client=client, clock=lambda: NOW).discover()
            self.assertEqual(len(calls), 2)
            self.assertEqual(discovery['dimensions']['year_type']['1'], 'Agriculture Year')
            self.assertEqual(
                discovery['responses'][0]['body_sha256'],
                BenchmarkResponse.capture(API_URL, raw('indicators')).body_sha256,
            )
            self.assertFalse(client.is_closed)
        client, calls = self.client()
        with client:
            snapshot = MoSPIProvider(client=client, clock=lambda: NOW).fetch(query())
            self.assertEqual(len(calls), 3)
            params = calls[-1].url.params
            self.assertEqual(params['indicator_code'], '6')
            self.assertEqual(params['frequency_code'], '1')
            self.assertEqual(params['limit'], str(PAGE_SIZE))
            self.assertEqual(params['year_type_code'], '1')
            self.assertEqual(params['year'], '2023-24')
            self.assertNotIn('quarter_code', params)
            self.assertIsNone(snapshot.released_at)
            self.assertEqual(snapshot.responses[-1].body, raw('values'))
            self.assertEqual(
                {r.response_id for r in snapshot.observations}, {snapshot.responses[-1].response_id}
            )
            self.assertEqual(len(snapshot.observations), 4)

    def paginated(self, *, mutate=None):
        filters = json.loads(raw('filters'))
        states = [{'state_code': v, 'description': f'Synthetic state {v}'} for v in range(1, 28)]
        filters['data']['state'] = states
        rows = [
            row(state=s['description'], quarter=quarter)
            for s in states
            for quarter in ('Jul-Sep', 'Oct-Dec', 'Jan-Mar', 'Apr-Jun')
        ]

        def data(request):
            page = int(request.url.params['page'])
            document = {
                'statusCode': True,
                'data': rows[(page - 1) * 100 : page * 100],
                'meta_data': {
                    'page': page,
                    'totalRecords': 108,
                    'totalPages': 2,
                    'recordPerPage': 100,
                },
            }
            if mutate:
                mutate(document, page)
            return json.dumps(document).encode()

        return self.client(data=data, filters=json.dumps(filters).encode()), query(
            state=tuple(str(v) for v in range(1, 28))
        )

    def test_complete_bounded_pagination_and_duplicate_detection(self):
        (client, calls), q = self.paginated()
        with client:
            snapshot = MoSPIProvider(client=client, clock=lambda: NOW).fetch(q)
        self.assertEqual(len(calls), 4)
        self.assertEqual(len(snapshot.responses), 4)
        self.assertEqual(len(snapshot.observations), 108)
        for mutate in (
            lambda d, p: d['meta_data'].update(totalRecords=109) if p == 2 else None,
            lambda d, p: (
                (d['meta_data'].update(totalRecords=107), d['data'].pop()) if p == 2 else None
            ),
            lambda d, p: d['data'].__setitem__(1, d['data'][0]) if p == 2 else None,
            lambda d, p: d['data'].pop() if p == 2 else None,
        ):
            (client, calls), q = self.paginated(mutate=mutate)
            with client, self.assertRaises(Unparseable):
                MoSPIProvider(client=client, clock=lambda: NOW).fetch(q)
            self.assertEqual(len(calls), 4)

    def test_failures_and_preflight(self):
        client, calls = self.client()
        with client, self.assertRaises(ConfigError):
            MoSPIProvider(client=client, clock=lambda: NOW).fetch(
                query().model_copy(update={'filters': {}})
            )
        self.assertEqual(calls, [])
        client, calls = self.client()
        with client, self.assertRaises(ConfigError):
            MoSPIProvider(client=client, clock=lambda: NOW).fetch(query(state=('98',)))
        self.assertEqual(len(calls), 2)
        client, _ = self.client()
        with client, self.assertRaisesRegex(Unparseable, 'omitted'):
            MoSPIProvider(client=client, clock=lambda: NOW).fetch(query(state=('99', '1')))
        cases: tuple[tuple[dict, type[Exception]], ...] = (
            ({'indicators': b'{}'}, Unparseable),
            ({'filters': b'\xff'}, Unparseable),
            ({'data': lambda r: b'{"statusCode":false}'}, Unparseable),
            (
                {'data': lambda r: b'{"statusCode":true,"data":[],"msg":"No Data Found"}'},
                Unparseable,
            ),
            ({'status': 403}, SourceError),
            ({'indicators': b' ' * (MAX_BYTES + 1)}, Unparseable),
            ({'unreachable': True}, Unreachable),
        )
        for settings, error in cases:
            client, _ = self.client(**settings)
            with client, self.assertRaises(error):
                MoSPIProvider(client=client, clock=lambda: NOW).fetch(query())

    def test_cli_atomicity_exports_and_offline_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'PLFS \u20b9'
            root.mkdir()
            path = root / 'query.json'
            path.write_bytes(raw('query'))
            database = root / 'applicant.db'
            export = root / 'snapshot.json'

            def cli(*args):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = main(['--no-log-file', '--data-dir', str(root), 'benchmark', *args])
                return code, out.getvalue()

            for action, args in (
                ('discover', ('--provider', 'mospi')),
                ('fetch', ('--query', str(path), '--export', str(export))),
            ):
                client, _ = self.client()

                def preflight(request):
                    self.assertFalse(database.exists())

                client.event_hooks['request'] = [preflight]
                with (
                    client,
                    mock.patch(
                        'applicant.services.benchmark_acquisition.MoSPIProvider',
                        return_value=MoSPIProvider(client=client, clock=lambda: NOW),
                    ),
                ):
                    code, text = cli(action, *args)
                self.assertEqual(code, 0)
            report = json.loads(text)
            self.assertEqual(json.loads(export.read_text(encoding='utf-8')), report)
            with Store.open_readonly(database) as store:
                snapshot = BenchmarkRepository(store.db).get(report['snapshot']['snapshot_id'])
                self.assertEqual(len(snapshot.observations), 4)
            before = database.read_bytes()
            client, _ = self.client(data=lambda r: b'{}')
            with (
                client,
                mock.patch(
                    'applicant.services.benchmark_acquisition.MoSPIProvider',
                    return_value=MoSPIProvider(client=client, clock=lambda: NOW),
                ),
            ):
                self.assertEqual(cli('fetch', '--query', str(path))[0], 5)
            self.assertEqual(database.read_bytes(), before)
            with mock.patch('httpx.Client.request', side_effect=AssertionError('network')):
                self.assertEqual(cli('show', '--snapshot', snapshot.snapshot_id)[0], 0)
            missing = root / 'missing.db'
            self.assertNotEqual(
                cli('show', '--database', str(missing), '--snapshot', snapshot.snapshot_id)[0], 0
            )
            self.assertFalse(missing.exists())

    def test_later_page_failure_never_opens_database(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'missing.db'
            from applicant.services.benchmark_acquisition import fetch_benchmarks

            (client, _), q = self.paginated(mutate=lambda d, p: d['data'].pop() if p == 2 else None)
            with (
                client,
                mock.patch(
                    'applicant.services.benchmark_acquisition.MoSPIProvider',
                    return_value=MoSPIProvider(client=client, clock=lambda: NOW),
                ),
                self.assertRaises(Unparseable),
            ):
                fetch_benchmarks(database, q)
            self.assertFalse(database.exists())
