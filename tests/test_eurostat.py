"""Eurostat cube mapping, exact provenance and acquisition-before-write journeys."""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from itertools import product
from pathlib import Path
from unittest import mock

import httpx
from hypothesis import given
from hypothesis import strategies as st

from applicant.cli import main
from applicant.domain.benchmarks import BenchmarkQuery, BenchmarkResponse
from applicant.domain.eurostat import DIMENSIONS, decode_cube, observations, resolve_query
from applicant.errors import ConfigError, SourceError, Unparseable
from applicant.infra.eurostat import URL, EurostatProvider
from applicant.infra.store.benchmarks import BenchmarkRepository
from applicant.infra.store.sqlite import Store
from applicant.services.benchmark_acquisition import discover_benchmarks, fetch_benchmarks
from tests.ranking_fixtures import NOW

FIXTURES = Path(__file__).parent / 'fixtures' / 'benchmarks'


def raw(name):
    return (FIXTURES / f'eurostat-{name}.json').read_bytes()


def query():
    return BenchmarkQuery.model_validate_json(raw('query'))


def encode(payload):
    return json.dumps(payload).encode()


class EurostatDomainTest(unittest.TestCase):
    def test_sparse_dense_global_flags_and_query_defaults(self):
        metadata = decode_cube(raw('metadata'))
        resolved = resolve_query(query(), metadata)
        cube = decode_cube(raw('values'))
        response = BenchmarkResponse.capture(URL, raw('values'))
        rows = observations(cube, resolved, response)
        self.assertEqual(
            [r.amount for r in rows], [Decimal('50000.125'), Decimal('73798.125'), None, Decimal(0)]
        )
        self.assertEqual(rows[2].status, 'suppressed')
        self.assertEqual(rows[2].flags, ('|C',))
        self.assertEqual(rows[3].flags, ('u',))
        self.assertEqual(rows[0].cohort.occupation_system, 'ISCO-08')
        payload = json.loads(raw('values'))
        payload['value'] = [50000.125, 73798.125, None, 0]
        payload['status'] = [None, None, '|C', 'u']
        for dim in payload['dimension'].values():
            dim['category']['index'] = list(dim['category']['index'])
        dense = observations(decode_cube(encode(payload)), resolved, response)
        self.assertEqual(dense, rows)
        payload['status'] = 'e'
        self.assertEqual(decode_cube(encode(payload)).statuses[2], 'e')
        self.assertEqual(
            observations(decode_cube(encode(payload)), resolved, response)[2].status, 'unavailable'
        )
        payload['status'] = {'0': 'c e', '1': 'e|F'}
        self.assertEqual(
            observations(decode_cube(encode(payload)), resolved, response)[0].status, 'suppressed'
        )
        self.assertEqual(
            observations(decode_cube(encode(payload)), resolved, response)[1].status, 'available'
        )

    @given(st.permutations(DIMENSIONS))
    def test_dimension_permutations_preserve_cell_meaning(self, order):
        payload = json.loads(raw('values'))
        original = decode_cube(raw('values'))
        resolved = resolve_query(query(), decode_cube(raw('metadata')))
        coords = list(product(*(original.codes[k] for k in original.ids)))
        lookup = {
            tuple(sorted(zip(original.ids, c, strict=True))): (
                original.values.get(i),
                original.statuses.get(i),
            )
            for i, c in enumerate(coords)
        }
        payload['id'] = list(order)
        payload['size'] = [len(original.codes[k]) for k in order]
        payload['value'], payload['status'] = {}, {}
        for i, c in enumerate(product(*(original.codes[k] for k in order))):
            value, flag = lookup[tuple(sorted(zip(order, c, strict=True)))]
            if value is not None:
                payload['value'][str(i)] = float(value)
            if flag:
                payload['status'][str(i)] = flag
        response = BenchmarkResponse.capture(URL, raw('values'))
        expected = observations(original, resolved, response)
        actual = observations(decode_cube(encode(payload)), resolved, response)
        self.assertEqual(
            sorted(expected, key=lambda r: r.observation_id),
            sorted(actual, key=lambda r: r.observation_id),
        )

    def test_malformed_cubes_fail_closed(self):
        base = json.loads(raw('values'))
        variants = [
            [],
            {'error': {}},
            {'warning': {}},
            base | {'version': '1.0'},
            base | {'id': ['geo', 'geo']},
            base | {'size': [True] * 9},
            base | {'size': [10**9] * 9},
            base | {'dimension': {}},
            base | {'value': []},
            base | {'value': {'4': 1}},
            base | {'value': {'-1': 1}},
            base | {'value': {'01': 1}},
            base | {'value': {'0': True}},
            base | {'value': {'0': -1}},
            base | {'status': {'0': 3}},
            base | {'status': False},
            base | {'extension': {'id': 'wrong'}},
            base | {'extension': {'id': None}},
        ]
        for mutate in (
            'missing_category',
            'bad_position',
            'bool_position',
            'duplicate_index',
            'bad_label',
        ):
            payload = copy.deepcopy(base)
            category = payload['dimension']['geo']['category']
            if mutate == 'missing_category':
                payload['dimension']['geo'] = None
            elif mutate == 'bad_position':
                category['index'] = {'DE': 0, 'FR': 0}
            elif mutate == 'bool_position':
                category['index'] = {'DE': False, 'FR': 1}
            elif mutate == 'duplicate_index':
                category['index'] = ['DE', 'DE']
            else:
                category['label'] = {'unknown': 'label'}
            variants.append(payload)
        for payload in variants:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                decode_cube(encode(payload))
        for payload in (b'{"a":1,"a":2}', b'{"value":NaN}', b'\xff', b' ' * 8_000_001):
            with self.assertRaises((ValueError, UnicodeError)):
                decode_cube(payload)

    def test_query_scope_and_exact_response_membership(self):
        metadata = decode_cube(raw('metadata'))
        for changes in (
            {'provider': 'other'},
            {'dataset': 'other'},
            {'filters': {}},
            {'filters': {'geo': ['DE'], 'isco08': ['OC2'], 'unit': ['PPS']}},
            {'filters': {'geo': ['DE'], 'isco08': ['OC25']}},
            {'filters': {'geo': ['DE'], 'isco08': ['OC2'], 'unknown': ['x']}},
        ):
            with self.assertRaises(ValueError):
                resolve_query(
                    BenchmarkQuery.model_validate(query().model_dump() | changes), metadata
                )
        with self.assertRaises(ValueError):
            resolve_query(
                query(),
                type(metadata)(
                    metadata.payload, ('geo',), metadata.codes, metadata.values, metadata.statuses
                ),
            )
        resolved = resolve_query(query(), metadata)
        changed = json.loads(raw('values'))
        changed['dimension']['geo']['category']['index'] = {'NL': 0, 'FR': 1}
        changed['dimension']['geo']['category']['label'] = {}
        with self.assertRaises(ValueError):
            observations(
                decode_cube(encode(changed)),
                resolved,
                BenchmarkResponse.capture(URL, raw('values')),
            )

    def test_fetch_cell_limits(self):
        metadata = decode_cube(raw('metadata'))
        resolved = resolve_query(query(), metadata)
        with mock.patch('applicant.domain.eurostat.MAX_FETCH_CELLS', 1):
            with self.assertRaises(ValueError):
                resolve_query(query(), metadata)
            with self.assertRaises(ValueError):
                observations(
                    decode_cube(raw('values')),
                    resolved,
                    BenchmarkResponse.capture(URL, raw('values')),
                )


class EurostatAcquisitionTest(unittest.TestCase):
    def client(self, second=None, status=200):
        calls = []

        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(200, content=raw('metadata'))
            return httpx.Response(status, content=raw('values') if second is None else second)

        return httpx.Client(transport=httpx.MockTransport(handler)), calls

    def test_discovery_fetch_raw_hashes_and_injected_client_ownership(self):
        client, calls = self.client()
        provider = EurostatProvider(client=client, clock=lambda: NOW)
        discovery = provider.discover()
        self.assertEqual(discovery['provider'], 'eurostat')
        self.assertIn('geo', discovery['dimensions'])
        client.close()
        client, calls = self.client()
        provider = EurostatProvider(client=client, clock=lambda: NOW)
        result = provider.fetch(query())
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1].url.params.get_list('geo'), ['DE', 'FR'])
        self.assertEqual(calls[1].url.params['unit'], 'EUR')
        self.assertEqual(result.responses[0].body, raw('metadata'))
        self.assertEqual(result.responses[1].body, raw('values'))
        self.assertEqual(len(result.observations), 4)
        self.assertIsNone(result.released_at)
        self.assertFalse(client.is_closed)
        client.close()

    def test_provider_errors_and_invalid_queries_never_return_partial_snapshots(self):
        for second, status, exception in (
            (b'{"warning":{"status":413}}', 200, Unparseable),
            (b'bad', 200, Unparseable),
            (b'', 400, SourceError),
            (b' ' * 8_000_001, 200, Unparseable),
        ):
            client, _ = self.client(second, status)
            with client, self.assertRaises(exception):
                EurostatProvider(client=client, clock=lambda: NOW).fetch(query())
        client, calls = self.client()
        with client:
            with self.assertRaises(ConfigError):
                EurostatProvider(client=client).fetch(
                    query().model_copy(update={'provider': 'wrong'})
                )
            self.assertEqual(calls, [])
            with self.assertRaises(ConfigError):
                EurostatProvider(client=client).fetch(query().model_copy(update={'filters': {}}))
            self.assertEqual(len(calls), 1)
        client, _ = self.client(encode(json.loads(raw('values')) | {'dimension': {}}))
        with client, self.assertRaises(Unparseable):
            EurostatProvider(client=client).fetch(query())
        changed = json.loads(raw('values'))
        changed['dimension']['geo']['category']['index'] = {'NL': 0, 'FR': 1}
        changed['dimension']['geo']['category']['label'] = {}
        client, _ = self.client(encode(changed))
        with client, self.assertRaises(Unparseable):
            EurostatProvider(client=client).fetch(query())

    def test_cli_acquisition_before_store_and_failure_atomicity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            qpath = root / 'query.json'
            qpath.write_bytes(raw('query'))
            database = root / 'applicant.db'

            def cli(*args):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = main(['--no-log-file', '--data-dir', str(root), 'benchmark', *args])
                return code, out.getvalue()

            client, _ = self.client(b'{"warning":{"status":413}}')
            with (
                client,
                mock.patch(
                    'applicant.services.benchmark_acquisition.EurostatProvider',
                    return_value=EurostatProvider(client=client, clock=lambda: NOW),
                ),
            ):
                self.assertEqual(cli('fetch', '--query', str(qpath))[0], 5)
                self.assertFalse(database.exists())

            client, _ = self.client()
            with (
                client,
                mock.patch(
                    'applicant.services.benchmark_acquisition.EurostatProvider',
                    return_value=EurostatProvider(client=client, clock=lambda: NOW),
                ),
            ):
                code, text = cli('discover', '--provider', 'eurostat')
                self.assertEqual(code, 0)
                self.assertFalse(database.exists())
            client, _ = self.client()

            def assert_before_request(request):
                self.assertFalse(database.exists())

            client.event_hooks['request'] = [assert_before_request]
            with (
                client,
                mock.patch(
                    'applicant.services.benchmark_acquisition.EurostatProvider',
                    return_value=EurostatProvider(client=client, clock=lambda: NOW),
                ),
            ):
                code, text = cli('fetch', '--query', str(qpath), '--export', 'acquired.json')
                self.assertEqual(code, 0)
            report = json.loads(text)
            with Store.open_readonly(database) as store:
                saved = BenchmarkRepository(store.db).get(report['snapshot']['snapshot_id'])
                self.assertEqual(saved.responses[1].body, raw('values'))
            self.assertEqual(
                json.loads((root / 'acquired.json').read_text())['snapshot']['snapshot_id'],
                saved.snapshot_id,
            )
            before = database.read_bytes()
            client, _ = self.client(b'{"error":{}}')
            with (
                client,
                mock.patch(
                    'applicant.services.benchmark_acquisition.EurostatProvider',
                    return_value=EurostatProvider(client=client, clock=lambda: NOW),
                ),
            ):
                self.assertEqual(cli('fetch', '--query', str(qpath))[0], 5)
            self.assertEqual(database.read_bytes(), before)
            for args in (
                ('discover',),
                ('fetch',),
                ('show', '--provider', 'eurostat'),
                ('discover', '--provider', 'unknown'),
                ('fetch', '--query', str(qpath), '--dry-run'),
                ('discover', '--provider', 'eurostat', '--query', str(qpath)),
                ('fetch', '--query', str(qpath), '--provider', 'eurostat'),
            ):
                with self.subTest(args=args), self.assertRaises(SystemExit) as stopped:
                    cli(*args)
                self.assertEqual(stopped.exception.code, 2)
            with self.assertRaises(ConfigError):
                discover_benchmarks('unknown')
            wrong = saved.model_copy(
                update={'query': saved.query.model_copy(update={'provider': 'other'})}
            )
            with mock.patch('applicant.services.benchmark_acquisition.provider') as factory:
                factory.return_value.fetch.return_value = wrong
                with self.assertRaises(ValueError):
                    fetch_benchmarks(database, query())
