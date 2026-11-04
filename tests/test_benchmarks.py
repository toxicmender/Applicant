"""Golden cohort arithmetic, immutable acquisition storage and offline CLI journeys."""

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal, localcontext
from pathlib import Path
from unittest import mock

from hypothesis import given
from hypothesis import strategies as st

from applicant.cli import main
from applicant.domain.benchmark_normalization import cohort_groups, normalize_observation
from applicant.domain.benchmarks import (
    BenchmarkCohort,
    BenchmarkComparisonDocument,
    BenchmarkObservation,
    BenchmarkQuery,
    BenchmarkResponse,
    BenchmarkSelection,
    BenchmarkSnapshot,
)
from applicant.domain.canonical import canonical_json
from applicant.domain.ranking import RankingRun
from applicant.errors import StoreError
from applicant.infra.store.benchmarks import BenchmarkNotFound, BenchmarkRepository
from applicant.infra.store.ranking_common import write_transaction
from applicant.infra.store.sqlite import MIGRATIONS, Store
from applicant.services.benchmarks import compare_benchmarks, import_snapshot, show_snapshots
from applicant.services.ranking_history import complete_run
from tests import test_ranking_cli as creation
from tests.test_ranking_contracts import fx

FIXTURES = Path(__file__).parent / 'fixtures' / 'benchmarks'


def snapshot(**changes):
    data = json.loads((FIXTURES / 'snapshot.json').read_text(encoding='utf-8'))
    return BenchmarkSnapshot.model_validate(data | changes)


class BenchmarkDomainTest(unittest.TestCase):
    def test_golden_normalization_and_cohort_separation(self):
        sample = snapshot()
        expected = json.loads((FIXTURES / 'expected.json').read_text())
        self.assertEqual(sample.snapshot_hash, expected['snapshot_hash'])
        self.assertEqual(BenchmarkSnapshot.model_validate_json(canonical_json(sample)), sample)
        value = sample.observations[0]
        self.assertEqual(
            normalize_observation(value, target_currency='EUR', fx=fx())['annual_amount'],
            Decimal(expected['annual_eur']),
        )
        self.assertEqual(
            normalize_observation(value, target_currency='INR', fx=fx())['annual_amount'],
            Decimal(expected['annual_inr']),
        )
        with localcontext() as context:
            context.prec = 2
            result = normalize_observation(value, target_currency='INR', fx=fx())
        self.assertEqual(result['annual_amount'], Decimal(expected['annual_inr']))
        self.assertTrue(result['annualized_equivalent'])
        self.assertEqual(len(cohort_groups((sample,))), expected['cohort_count'])
        self.assertEqual(len(cohort_groups((sample, snapshot(snapshot_id='second')))), 3)
        self.assertEqual(cohort_groups(()), [])

    def test_unit_assumptions_unknown_censored_and_missing_fx(self):
        value = snapshot().observations[0]
        for period, options, annual in (
            ('annual', {}, '1000'),
            ('daily', {'annual_days': Decimal(200)}, '200000'),
            ('hourly', {'annual_hours': Decimal(1800)}, '1800000'),
        ):
            observation = value.model_copy(update={'period': period})
            result = normalize_observation(observation, target_currency='EUR', fx=fx(), **options)
            self.assertEqual(result['annual_amount'], Decimal(annual))
        unknown = normalize_observation(
            value.model_copy(update={'period': 'hourly'}), target_currency='EUR', fx=fx()
        )
        self.assertIn('working_time_assumption_required', unknown['reasons'])
        missing = normalize_observation(value, target_currency='USD', fx=fx())
        self.assertIsNone(missing['annual_amount'])
        self.assertIn('fx_rate_unavailable', missing['reasons'])
        for observation in (
            snapshot().observations[2],
            value.model_copy(update={'status': 'censored'}),
        ):
            self.assertEqual(
                normalize_observation(observation, target_currency='EUR', fx=fx())['status'],
                'context_only',
            )
        for options in ({'annual_hours': Decimal(0)}, {'annual_days': Decimal('NaN')}):
            with self.assertRaises(ValueError):
                normalize_observation(value, target_currency='EUR', fx=fx(), **options)
        with self.assertRaisesRegex(ValueError, 'supported range'):
            normalize_observation(
                value.model_copy(update={'amount': Decimal('1e100')}),
                target_currency='EUR',
                fx=fx(),
            )
        with self.assertRaisesRegex(ValueError, 'supported range'):
            normalize_observation(
                value, target_currency='INR', fx=fx(rates={'EUR': 1, 'INR': '1e-99999'})
            )

    @given(st.integers(min_value=0, max_value=10**9))
    def test_monthly_and_annual_equivalence(self, amount):
        value = snapshot().observations[0].model_copy(update={'amount': Decimal(amount)})
        annual = value.model_copy(update={'amount': Decimal(amount) * 12, 'period': 'annual'})
        a = normalize_observation(value, target_currency='INR', fx=fx())
        b = normalize_observation(annual, target_currency='INR', fx=fx())
        self.assertEqual(a['annual_amount'], b['annual_amount'])

    def test_strict_contracts_and_digest_validation(self):
        sample = snapshot()
        response = sample.responses[0]
        self.assertEqual(BenchmarkResponse.capture(response.source_url, response.body), response)
        self.assertEqual(response.body, b'{"monthly_base":1000,"earnings":1500}')
        for changes in ({'body_base64': '!'}, {'body_sha256': '0' * 64}):
            with self.assertRaises(ValueError):
                BenchmarkResponse.model_validate(response.model_dump() | changes)
        for filters in ({'year': []}, {'year': ['2022', '2022']}, {' ': ['2022']}):
            with self.assertRaises(ValueError):
                BenchmarkQuery.model_validate({'provider': 'x', 'dataset': 'x', 'filters': filters})
        for changes in ({'reference_end': '2020-01-01'}, {'dimensions': {' ': 'bad'}}):
            with self.assertRaises(ValueError):
                BenchmarkCohort.model_validate(sample.observations[0].cohort.model_dump() | changes)
        for changes in (
            {'amount': None},
            {'status': 'suppressed'},
            {'amount': 'NaN'},
            {'amount': -1},
        ):
            with self.assertRaises(ValueError):
                BenchmarkObservation.model_validate(sample.observations[0].model_dump() | changes)
        for changes in (
            {'responses': []},
            {'responses': [response, response]},
            {'observations': [sample.observations[0], sample.observations[0]]},
            {
                'observations': [
                    sample.observations[0].model_copy(update={'response_id': 'missing'})
                ]
            },
            {'released_at': '2030-01-01T00:00:00Z'},
        ):
            with self.assertRaises(ValueError):
                BenchmarkSnapshot.model_validate(sample.model_dump() | changes)
        selection = BenchmarkSelection(
            job_id='job',
            snapshot_id=sample.snapshot_id,
            observation_id='base',
            expected_cohort=sample.observations[0].cohort,
        )
        with self.assertRaises(ValueError):
            BenchmarkComparisonDocument(selections=(selection, selection))


class BenchmarkStoreTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'bench # 测试.db'

    def test_dry_run_idempotency_sharing_and_immutable_completion(self):
        sample = snapshot()
        self.assertEqual(import_snapshot(self.path, sample, dry_run=True), sample)
        self.assertFalse(self.path.exists())
        self.assertEqual(import_snapshot(self.path, sample), sample)
        before = self.path.read_bytes()
        self.assertEqual(import_snapshot(self.path, sample, dry_run=True), sample)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(import_snapshot(self.path, sample), sample)
        import_snapshot(self.path, snapshot(snapshot_id='second'))
        for dry_run in (True, False):
            with self.assertRaises((ValueError, StoreError)):
                import_snapshot(self.path, snapshot(adapter_version='changed'), dry_run=dry_run)
        self.assertEqual(len(show_snapshots(self.path, ('golden-wages', 'second'))['cohorts']), 3)
        with Store(self.path) as store:
            repo = BenchmarkRepository(store.db)
            self.assertEqual(
                store.db.execute('SELECT count(*) FROM benchmark_responses').fetchone()[0], 1
            )
            with self.assertRaises(StoreError):
                repo.put(sample)
            for table in (
                'benchmark_responses',
                'benchmark_snapshots',
                'benchmark_snapshot_responses',
                'benchmark_observations',
                'benchmark_completions',
            ):
                for sql in (
                    f'DELETE FROM {table}',
                    f'UPDATE {table} SET rowid=rowid',
                    f'INSERT OR REPLACE INTO {table} SELECT * FROM {table}',
                ):
                    with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                        store.db.execute(sql)
                    store.db.rollback()
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'completed'):
                store.db.execute(
                    "INSERT INTO benchmark_observations VALUES ('golden-wages','late',?,4,'{}')",
                    (sample.responses[0].response_id,),
                )
            store.db.rollback()
            with self.assertRaises(BenchmarkNotFound):
                repo.get('missing')
        for ids in ((), ('golden-wages', 'golden-wages')):
            with self.assertRaises(ValueError):
                show_snapshots(self.path, ids)

    def test_each_upgrade_and_rollback(self):
        for version in range(1, len(MIGRATIONS)):
            path = self.path.with_name(f'old-{version}.db')
            with (
                mock.patch('applicant.infra.store.sqlite.MIGRATIONS', MIGRATIONS[:version]),
                Store(path),
            ):
                pass
            with Store(path) as upgraded:
                self.assertEqual(
                    upgraded.db.execute('PRAGMA user_version').fetchone()[0], len(MIGRATIONS)
                )
        import_snapshot(self.path, snapshot())
        with Store(self.path) as store:
            with self.assertRaises(RuntimeError), write_transaction(store.db):
                BenchmarkRepository(store.db).put(snapshot(snapshot_id='rolled-back'))
                raise RuntimeError('fail after completion')
            with self.assertRaises(BenchmarkNotFound):
                BenchmarkRepository(store.db).get('rolled-back')
            with self.assertRaises(sqlite3.IntegrityError):
                store.db.execute(
                    "INSERT INTO benchmark_snapshot_responses VALUES ('missing','missing',0)"
                )
            store.db.rollback()

    def test_corruption_of_metadata_children_and_raw_responses(self):
        for sql in (
            "UPDATE benchmark_snapshots SET snapshot_hash='bad'",
            "UPDATE benchmark_snapshots SET payload='[]'",
            "UPDATE benchmark_snapshots SET payload='{}'",
            "UPDATE benchmark_snapshots SET payload=json_set(payload,'$.observations',json('[]'))",
            "UPDATE benchmark_observations SET ordinal=9 WHERE observation_id='base'",
            "UPDATE benchmark_observations SET observation_id='wrong' WHERE observation_id='base'",
            "UPDATE benchmark_observations SET payload='{}' WHERE observation_id='base'",
            'UPDATE benchmark_snapshot_responses SET ordinal=2',
            "UPDATE benchmark_responses SET payload='{}'",
            "UPDATE benchmark_responses SET payload=json_set(payload,'$.source_url','https://different.invalid')",
        ):
            import_snapshot(self.path, snapshot())
            with Store(self.path) as store:
                table = sql.split()[1]
                store.db.execute(f'DROP TRIGGER immutable_{table}_update')
                store.db.execute(sql)
                store.db.commit()
                with self.assertRaises(StoreError):
                    BenchmarkRepository(store.db).get('golden-wages')
            self.path.unlink()

    def test_failed_schema_six_migration_is_atomic(self):
        with (
            mock.patch('applicant.infra.store.sqlite.MIGRATIONS', MIGRATIONS[:5]),
            Store(self.path),
        ):
            pass
        broken = (*MIGRATIONS[:5], (*MIGRATIONS[5], 'INVALID SQL'))
        with (
            mock.patch('applicant.infra.store.sqlite.MIGRATIONS', broken),
            self.assertRaises(StoreError),
        ):
            Store(self.path)
        db = sqlite3.connect(self.path)
        try:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 5)
            self.assertIsNone(
                db.execute(
                    "SELECT name FROM sqlite_master WHERE name='benchmark_snapshots'"
                ).fetchone()
            )
        finally:
            db.close()


class BenchmarkCliTest(unittest.TestCase):
    def setUp(self):
        self.data = creation.RankingCliTest()
        self.data.setUp()
        self.addCleanup(self.data.doCleanups)
        self.path = self.data.database
        self.created = self.data.service(versions_path=self.data.bundle(self.data.mixed_jobs()))
        self.sample = snapshot()
        self.input = self.data.write('snapshot.json', self.sample)

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(['--no-log-file', '--data-dir', str(self.data.root), 'benchmark', *args])
        return code, out.getvalue(), err.getvalue()

    def selection(self, **changes):
        return BenchmarkSelection.model_validate(
            {
                'job_id': self.created.results[0].job_id,
                'snapshot_id': self.sample.snapshot_id,
                'observation_id': 'base',
                'expected_cohort': self.sample.observations[0].cohort,
            }
            | changes
        )

    def test_offline_journey_and_frozen_comparison(self):
        before = self.path.read_bytes()
        self.assertEqual(self.cli('import', '--from-file', str(self.input), '--dry-run')[0], 0)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.cli('import', '--from-file', str(self.input))[0], 0)
        selections = self.data.write(
            'selection.json', BenchmarkComparisonDocument(selections=(self.selection(),))
        )
        before = self.path.read_bytes()
        with mock.patch('httpx.Client.request', side_effect=AssertionError('network')):
            code, text, _ = self.cli(
                'compare',
                '--run',
                self.created.ranking_run_id,
                '--selections',
                str(selections),
                '--export',
                'report.json',
            )
            self.assertEqual(code, 0)
            report = json.loads(text)
            self.assertTrue(report['results'][0]['comparable'])
            self.assertEqual(report, json.loads((self.data.root / 'report.json').read_text()))
            self.assertEqual(self.cli('show', '--snapshot', 'golden-wages', '--json')[0], 0)
            self.assertEqual(
                self.cli('show', '--snapshot', 'golden-wages', '--snapshot', 'golden-wages')[0], 2
            )
        self.assertEqual(self.path.read_bytes(), before)
        import_snapshot(self.path, snapshot(snapshot_id='new-release', release_label='new'))
        self.assertEqual(
            json.loads(
                self.cli(
                    'compare', '--run', self.created.ranking_run_id, '--selections', str(selections)
                )[1]
            ),
            report,
        )

    def test_context_mismatches_and_missing_selections(self):
        import_snapshot(self.path, self.sample)
        net = self.sample.observations[0].model_copy(
            update={
                'cohort': self.sample.observations[0].cohort.model_copy(update={'basis': 'net'})
            }
        )
        import_snapshot(self.path, snapshot(snapshot_id='net', observations=(net,)))
        net_report = compare_benchmarks(
            self.path,
            self.created.ranking_run_id,
            BenchmarkComparisonDocument(
                selections=(self.selection(snapshot_id='net', expected_cohort=net.cohort),)
            ),
        )
        self.assertIn('salary_basis_mismatch', net_report['results'][0]['reasons'])
        for changes, reason in (
            ({'observation_id': 'earnings'}, 'wage_definition_mismatch'),
            ({'observation_id': 'suppressed'}, 'suppressed'),
            (
                {
                    'expected_cohort': self.sample.observations[0].cohort.model_copy(
                        update={'basis': 'net'}
                    )
                },
                'cohort_mismatch',
            ),
        ):
            report = compare_benchmarks(
                self.path,
                self.created.ranking_run_id,
                BenchmarkComparisonDocument(selections=(self.selection(**changes),)),
            )
            self.assertIn(reason, report['results'][0]['reasons'])
            self.assertIsNone(report['results'][0]['gap'])
        source = self.data.mixed_jobs()[0]
        unknown = type(source).model_validate(
            source.model_dump()
            | {
                'job_version_id': 'unknown-pay',
                'job_data': source.job_data | {'id': 'unknown-pay', 'salary': None},
                'evidence': [],
            }
        )
        run = self.data.service(versions_path=self.data.bundle((unknown,)))
        report = compare_benchmarks(
            self.path,
            run.ranking_run_id,
            BenchmarkComparisonDocument(selections=(self.selection(job_id=run.results[0].job_id),)),
        )
        self.assertIn('job_compensation_unavailable', report['results'][0]['reasons'])
        for changes, exception in (
            ({'job_id': 'absent'}, ValueError),
            ({'observation_id': 'absent'}, BenchmarkNotFound),
        ):
            with self.assertRaises(exception):
                compare_benchmarks(
                    self.path,
                    self.created.ranking_run_id,
                    BenchmarkComparisonDocument(selections=(self.selection(**changes),)),
                )
        legacy = RankingRun.model_validate(
            self.created.model_dump(
                exclude={
                    'selected_tiers',
                    'engine_policy',
                    'results',
                    'resolution_diagnostics',
                    'schema_version',
                }
            )
            | {'ranking_run_id': 'legacy', 'results': ()}
        )
        with Store(self.path) as store:
            complete_run(store, legacy)
        with self.assertRaises(ValueError):
            compare_benchmarks(self.path, 'legacy', BenchmarkComparisonDocument(selections=()))

    def test_cli_strict_inputs_errors_and_export_guard(self):
        for options in (
            ('import',),
            ('show',),
            ('compare',),
            ('show', '--snapshot', 'x', '--dry-run'),
            ('import', '--from-file', str(self.input), '--run', 'x'),
            ('compare', '--run', 'x', '--selections', 'x', '--snapshot', 'x'),
        ):
            self.assertEqual(self.cli(*options)[0], 2)
        self.assertEqual(self.cli('show', '--snapshot', 'x', '--database', 'absent.db')[0], 4)
        self.assertEqual(self.cli('show', '--snapshot', 'missing')[0], 4)
        self.input.write_text('{"kind":1,"kind":2}', encoding='utf-8')
        self.assertEqual(self.cli('import', '--from-file', str(self.input))[0], 2)
        self.input.write_text(canonical_json(self.sample), encoding='utf-8')
        self.assertEqual(
            self.cli('import', '--from-file', str(self.input), '--export', str(self.path))[0], 2
        )
        with Store.open_readonly(self.path) as store:
            self.assertEqual(
                store.db.execute('SELECT count(*) FROM benchmark_snapshots').fetchone()[0], 0
            )
        import_snapshot(self.path, self.sample)
        with mock.patch('applicant.cli.benchmark.write_document', side_effect=OSError('disk')):
            self.assertEqual(
                self.cli('show', '--snapshot', 'golden-wages', '--export', 'failed.json')[0], 1
            )
