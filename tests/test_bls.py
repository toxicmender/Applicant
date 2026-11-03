"""BLS mapping, bounded request construction and failure-atomic acquisition."""

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
from applicant.domain.bls import (
    TABLES,
    parse_observations,
    parse_table,
    series_plan,
    validate_query,
)
from applicant.errors import ConfigError, SourceError, Unparseable
from applicant.infra.bls import API_URL, BLSProvider
from applicant.infra.store.benchmarks import BenchmarkRepository
from applicant.infra.store.sqlite import Store
from tests.ranking_fixtures import NOW

FIXTURES = Path(__file__).parent / 'fixtures' / 'benchmarks'


def raw(name):
    return (FIXTURES / f'bls-{name}').read_bytes()


def query():
    return BenchmarkQuery.model_validate_json(raw('query.json'))


def tables():
    return {name: parse_table(raw(name + '.tsv'), name) for name in TABLES}


def response(payload):
    return BenchmarkResponse.capture(API_URL, json.dumps(payload).encode())


class BLSDomainTest(unittest.TestCase):
    def resolved(self):
        return validate_query(query(), as_of=NOW.date())

    def test_official_layout_and_decimal_values(self):
        resolved = self.resolved()
        plan = series_plan(resolved, tables())
        self.assertEqual(list(plan), ['OEUN000000000000015125204', 'OEUN000000000000015125213'])
        rows = parse_observations(
            BenchmarkResponse.capture(API_URL, raw('values.json')), resolved, plan
        )
        self.assertEqual([r.amount for r in rows], [Decimal('100000.25'), Decimal(95000)])
        self.assertEqual(rows[1].flags, ('P',))
        self.assertEqual(rows[0].cohort.occupation_system, 'SOC-2018')
        self.assertEqual(rows[0].cohort.reference_start, date(2025, 5, 1))
        self.assertEqual(rows[0].currency, 'USD')
        self.assertEqual(rows[0].period, 'annual')
        all_statistics = validate_query(
            BenchmarkQuery.model_validate(
                query().model_dump()
                | {
                    'filters': query().filters
                    | {'statistic': ('mean', 'p10', 'p25', 'median', 'p75', 'p90')}
                }
            ),
            as_of=NOW.date(),
        )
        self.assertEqual(
            [key[-2:] for key in series_plan(all_statistics, tables())],
            ['04', '11', '12', '13', '14', '15'],
        )
        data = json.loads(raw('values.json'))
        data['Results'] = [data['Results']]
        data['Results'][0]['series'].reverse()
        self.assertEqual(
            parse_observations(response(data), resolved, plan),
            tuple(r.model_copy(update={'response_id': response(data).response_id}) for r in rows),
        )

    def test_suppression_missing_and_censoring(self):
        resolved = self.resolved()
        plan = series_plan(resolved, tables())
        for value, notes, status, amount in (
            ('*', [], 'suppressed', None),
            ('-', [], 'unavailable', None),
            ('...', [], 'unavailable', None),
            ('#', [], 'censored', None),
            ('239200+', [], 'censored', Decimal(239200)),
            (
                '239200',
                [{'code': '5', 'text': 'Equal to or greater than the published threshold'}],
                'censored',
                Decimal(239200),
            ),
            ('0', [{'text': 'Estimate not released.'}], 'suppressed', None),
        ):
            data = json.loads(raw('values.json'))
            row = data['Results']['series'][0]['data'][0]
            row.update(value=value, footnotes=notes)
            observation = parse_observations(response(data), resolved, plan)[0]
            self.assertEqual((observation.status, observation.amount), (status, amount))

    @given(st.integers(min_value=0, max_value=10**9))
    def test_numeric_values_are_preserved_without_float_conversion(self, value):
        data = json.loads(raw('values.json'))
        data['Results']['series'][0]['data'][0]['value'] = str(value) + '.125'
        rows = parse_observations(
            response(data), self.resolved(), series_plan(self.resolved(), tables())
        )
        self.assertEqual(rows[0].amount, Decimal(value) + Decimal('.125'))

    def test_query_preflight_and_metadata_validation(self):
        base = query().model_dump()
        for changes in (
            {'provider': 'other'},
            {'dataset': 'other'},
            {'filters': {}},
            {'filters': query().filters | {'year': ('2025', '2024')}},
            {'filters': query().filters | {'year': ('2020',)}},
            {'filters': query().filters | {'year': ('2099',)}},
            {'filters': query().filters | {'year': ('bad',)}},
            {'filters': query().filters | {'area': ('0000000',)}},
            {'filters': query().filters | {'statistic': ('hourly',)}},
            {'filters': query().filters | {'extra': ('x',)}},
            {
                'filters': query().filters
                | {'occupation': tuple(str(n).zfill(6) for n in range(26))}
            },
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_query(BenchmarkQuery.model_validate(base | changes), as_of=NOW.date())
        with self.assertRaises(ValueError):
            series_plan(
                self.resolved().model_copy(
                    update={'filters': self.resolved().filters | {'occupation': ('000000',)}}
                ),
                tables(),
            )
        changed = tables()
        changed['datatype']['04']['datatype_name'] = 'Hourly mean wage'
        with self.assertRaises(ValueError):
            series_plan(self.resolved(), changed)
        for name in TABLES:
            for content in (
                b'',
                b'wrong\theader\n',
                raw(name + '.tsv') + b'bad\trow\n',
                raw(name + '.tsv') + raw(name + '.tsv').splitlines()[1] + b'\n',
            ):
                with self.subTest(name=name, content=content), self.assertRaises(ValueError):
                    parse_table(content, name)
        for content, name in (
            (b'area_code\tarea_name\tstate_code\tareatype_code\n1234567\tArea\tXX\tN\n', 'area'),
            (b'datatype_code\tdatatype_name\nxx\tLabel\n', 'datatype'),
            (b'industry_code\tindustry_name\tselectable\n000000\tAll\tY\n', 'industry'),
            (b'datatype_code\tdatatype_name\n', 'datatype'),
            (b'\xff', 'area'),
            (b' ' * 4_000_001, 'area'),
        ):
            with self.assertRaises((ValueError, UnicodeError)):
                parse_table(content, name)
        # Preserve publisher padding, trailing TSV columns and range codes.
        self.assertIn(
            '04',
            parse_table(b' datatype_code \tdatatype_name\t\n04\tAnnual mean wage\t\n', 'datatype'),
        )
        self.assertIn(
            '31--33',
            parse_table(
                b'industry_code\tindustry_name\tselectable\n31--33\tManufacturing\tT\n', 'industry'
            ),
        )
        self.assertIn(
            '04',
            parse_table(b'datatype_code\tdatatype_name\n\n\t\n04\tAnnual mean wage\n', 'datatype'),
        )

    def test_incomplete_or_malformed_api_responses_fail(self):
        base = json.loads(raw('values.json'))
        variants = [
            [],
            base | {'status': 'REQUEST_FAILED'},
            base | {'message': ['No Data Available']},
            base | {'Results': {}},
            base | {'Results': {'series': []}},
            base | {'Results': [{}, {}]},
        ]
        for mutate in (
            'duplicate',
            'unexpected',
            'missing',
            'duplicated_row',
            'wrong_year',
            'wrong_period',
            'bad_footnotes',
            'bad_footnote',
            'bad_value',
            'negative',
        ):
            data = copy.deepcopy(base)
            series = data['Results']['series']
            row = series[0]['data'][0]
            if mutate == 'duplicate':
                series[1] = copy.deepcopy(series[0])
            elif mutate == 'unexpected':
                series[0]['seriesID'] = 'wrong'
            elif mutate == 'missing':
                series[0]['data'] = []
            elif mutate == 'duplicated_row':
                series[0]['data'] = [row, row]
            elif mutate == 'wrong_year':
                row['year'] = '2024'
            elif mutate == 'wrong_period':
                row['period'] = 'M05'
            elif mutate == 'bad_footnotes':
                row['footnotes'] = {}
            elif mutate == 'bad_footnote':
                row['footnotes'] = [{'code': 1}]
            elif mutate == 'bad_value':
                row['value'] = 1
            else:
                row['value'] = '-10'
            variants.append(data)
        plan = series_plan(self.resolved(), tables())
        for data in variants:
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse_observations(response(data), self.resolved(), plan)
        for body in (b'{"status":1,"status":2}', b'{"x":NaN}', b'\xff', b' ' * 4_000_001):
            with self.assertRaises((ValueError, UnicodeError)):
                parse_observations(BenchmarkResponse.capture(API_URL, body), self.resolved(), plan)


class BLSAcquisitionTest(unittest.TestCase):
    def client(self, *, api=None, status=200, bad_table=None):
        calls = []

        def handler(request):
            calls.append(request)
            if request.method == 'GET':
                name = request.url.path.split('.')[-1]
                return httpx.Response(
                    200, content=b'bad' if name == bad_table else raw(name + '.tsv')
                )
            return httpx.Response(status, content=raw('values.json') if api is None else api)

        return httpx.Client(transport=httpx.MockTransport(handler)), calls

    def test_discovery_and_bounded_fetch(self):
        client, calls = self.client()
        with client:
            discovery = BLSProvider(client=client, clock=lambda: NOW).discover()
            self.assertEqual(len(calls), 4)
            self.assertIn('N:0000000', discovery['dimensions']['area'])
            self.assertFalse(client.is_closed)
        client, calls = self.client()
        with client:
            snapshot = BLSProvider(client=client, clock=lambda: NOW).fetch(query())
            body = json.loads(calls[-1].content)
            self.assertEqual(body['startyear'], body['endyear'])
            self.assertEqual(body['startyear'], '2025')
            self.assertEqual(len(body['seriesid']), 2)
            self.assertEqual(len(calls), 5)
            self.assertEqual(len(snapshot.responses), 5)
            self.assertEqual(snapshot.responses[-1].body, raw('values.json'))
            self.assertIsNone(snapshot.released_at)

    def test_failures_and_preflight(self):
        client, calls = self.client()
        with client, self.assertRaises(ConfigError):
            BLSProvider(client=client, clock=lambda: NOW).fetch(
                query().model_copy(update={'filters': {}})
            )
        self.assertEqual(calls, [])
        client, _ = self.client(bad_table='area')
        with client, self.assertRaises(Unparseable):
            BLSProvider(client=client, clock=lambda: NOW).fetch(query())
        client, _ = self.client()
        with client, self.assertRaises(ConfigError):
            BLSProvider(client=client, clock=lambda: NOW).fetch(
                query().model_copy(
                    update={'filters': query().filters | {'occupation': ('999999',)}}
                )
            )
        for api, status, error in (
            (b'{}', 200, Unparseable),
            (None, 403, SourceError),
            (b' ' * 4_000_001, 200, Unparseable),
        ):
            client, _ = self.client(api=api, status=status)
            with client, self.assertRaises(error):
                BLSProvider(client=client, clock=lambda: NOW).fetch(query())

    def test_cli_atomicity_offline_history_and_factory_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'query.json'
            path.write_bytes(raw('query.json'))
            database = root / 'applicant.db'

            def cli(*args):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = main(['--no-log-file', '--data-dir', str(root), 'benchmark', *args])
                return code, out.getvalue()

            for action, args in (
                ('discover', ('--provider', 'bls')),
                ('fetch', ('--query', str(path))),
            ):
                client, _ = self.client()

                def preflight(request):
                    self.assertFalse(database.exists())

                client.event_hooks['request'] = [preflight]
                with (
                    client,
                    mock.patch(
                        'applicant.services.benchmark_acquisition.BLSProvider',
                        return_value=BLSProvider(client=client, clock=lambda: NOW),
                    ),
                ):
                    code, text = cli(action, *args)
                    self.assertEqual(code, 0)
            report = json.loads(text)
            with Store.open_readonly(database) as store:
                snapshot = BenchmarkRepository(store.db).get(report['snapshot']['snapshot_id'])
                self.assertEqual(len(snapshot.observations), 2)
            before = database.read_bytes()
            client, _ = self.client(api=b'{"status":"REQUEST_FAILED"}')
            with (
                client,
                mock.patch(
                    'applicant.services.benchmark_acquisition.BLSProvider',
                    return_value=BLSProvider(client=client, clock=lambda: NOW),
                ),
            ):
                self.assertEqual(cli('fetch', '--query', str(path))[0], 5)
            self.assertEqual(database.read_bytes(), before)
            with mock.patch('httpx.Client.request', side_effect=AssertionError('network')):
                self.assertEqual(cli('show', '--snapshot', snapshot.snapshot_id)[0], 0)
