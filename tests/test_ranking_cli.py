"""Ranking command preflight, immutable membership and grouped output journeys."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from applicant.cli import main
from applicant.domain.canonical import canonical_json
from applicant.domain.ranking import Evidence, JobVersion
from applicant.domain.ranking_facts import RankingFacts
from applicant.domain.ranking_inputs import JobVersionBundle
from applicant.domain.ranking_v2 import RankingRunV3, ScoringConfigurationV2
from applicant.errors import ConfigError, StoreError
from applicant.infra.ranking_documents import read_ranking_document
from applicant.infra.store.ranking_runs import RankingRunRepository
from applicant.infra.store.sqlite import Store
from applicant.services.preferences import save_profile
from applicant.services.ranking_cli import create_ranking_run
from applicant.services.ranking_history import load_history
from applicant.services.ranking_replay import replay_run
from tests.ranking_fixtures import FIXTURES, NOW, compensation_facts
from tests.test_eligibility import candidate, employment
from tests.test_ranking_engine import version
from tests.test_work_routes import route


class RankingCliTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.database = self.root / 'applicant.db'
        self.provenance = self.root / 'provenance.json'
        self.provenance.write_bytes((FIXTURES / 'provenance.json').read_bytes())
        self.person = candidate(
            profile_id='default', minimum_base_salary=60000, preferred_base_salary=100000
        )
        with Store(self.database) as store:
            save_profile(store, self.person, expected_revision=0)
        self.listing = self.root / 'job_listing.json'
        self.listing.write_text('{"list": []}', encoding='utf-8')

    def write(self, name, data):
        path = self.root / name
        path.write_text(canonical_json(data), encoding='utf-8')
        return path

    def bundle(self, jobs):
        return self.write(
            'versions.json', {'kind': 'ranking_job_versions', 'schema_version': 1, 'jobs': jobs}
        )

    def cli(self, *options):
        out, err = io.StringIO(), io.StringIO()
        with (
            redirect_stdout(out),
            redirect_stderr(err),
            mock.patch('applicant.cli.rank.now', return_value=NOW),
        ):
            code = main(
                [
                    '--no-log-file',
                    '--data-dir',
                    str(self.root),
                    'rank',
                    'create',
                    '--provenance',
                    'provenance.json',
                    *options,
                ]
            )
        return code, out.getvalue(), err.getvalue()

    def service(self, **changes):
        return create_ranking_run(
            self.database,
            **(
                {
                    'listing': self.listing,
                    'provenance_path': self.provenance,
                    'clock': lambda: NOW,
                    'offline': True,
                }
                | changes
            ),
        )

    def mixed_jobs(self):
        def facts(amount):
            return RankingFacts(
                compensation=(
                    compensation_facts(
                        minimum=amount, maximum=amount, currency='EUR', period='annual'
                    ),
                ),
                routes=(route(),),
                routes_complete=True,
                employment=(employment(('permanent',)),),
            )

        return (
            version('eligible', bundles=(facts(90000),)),
            version('review'),
            version('excluded', bundles=(facts(100),)),
        )

    def test_mixed_batch_json_complete_membership_and_offline_replay(self):
        jobs = self.mixed_jobs()
        self.bundle(jobs)
        from applicant.services.ranking import evaluate_jobs

        with mock.patch(
            'applicant.services.ranking.evaluate_jobs', wraps=evaluate_jobs
        ) as evaluate:
            code, out, err = self.cli(
                '--job-versions', 'versions.json', '--offline', '--json', '--top', '1'
            )
        self.assertEqual(code, 0, err)
        self.assertEqual(evaluate.call_count, 1)
        report = json.loads(out)
        self.assertEqual(report['kind'], 'ranking_report')
        self.assertEqual([v['total'] for v in report['groups'].values()], [1, 1, 1])
        for state, group in report['groups'].items():
            self.assertEqual(group['results'][0]['eligibility'], state)
        self.assertIsNone(report['groups']['excluded']['results'][0]['overall_score'])
        run_id = report['run']['ranking_run_id']
        with Store.open_readonly(self.database) as store, store.read_snapshot():
            history = load_history(store, run_id)
            self.assertEqual(tuple(j.job_id for j in history.jobs), tuple(j.job_id for j in jobs))
            self.assertEqual(history.run.profile_revision, 1)
            self.assertTrue(
                replay_run(
                    store, run_id, build_identity='retained-test-artifact', lockfile_digest='a' * 64
                ).matches
            )

    def test_display_limit_is_not_persisted_and_null_scores_are_unscored(self):
        self.bundle((version('a'), version('b'), version('c')))
        code, out, err = self.cli('--job-versions', 'versions.json', '--offline', '--top', '1')
        self.assertEqual(code, 0, err)
        self.assertIn('review_required: 1 displayed / 3 total', out)
        self.assertIn('fit=unscored', out)
        self.assertIn('evidence support', out)
        with Store.open_readonly(self.database) as store:
            self.assertEqual(len(RankingRunRepository(store.db).completed()[0].results), 3)
        report = json.loads(
            self.cli('--job-versions', 'versions.json', '--offline', '--all', '--json')[1]
        )
        self.assertEqual(report['groups']['review_required']['displayed'], 3)

    def test_listing_capture_uses_injected_time_and_does_not_sync_or_restore_file(self):
        self.write(
            'job_listing.json',
            {
                'list': [
                    {
                        'source': 'fixture',
                        'id': '1',
                        'title': 'Developer\u001b[2J',
                        'salary': 'EUR 90000 gross base per year',
                        'employment_type': 'permanent',
                        'remote': True,
                        'flags': ['transient'],
                    }
                ]
            },
        )
        original = self.listing.read_bytes()
        code, out, err = self.cli('--offline')
        self.assertEqual(code, 0, err)
        self.assertNotIn('\u001b', out)
        with Store.open_readonly(self.database) as store:
            history = load_history(
                store, RankingRunRepository(store.db).completed()[0].ranking_run_id
            )
            self.assertEqual(history.jobs[0].captured_at, NOW)
            self.assertEqual(history.jobs[0].parser_version, 'cli-listing-1')
            self.assertNotIn('flags', history.jobs[0].job_data)
            self.assertEqual(store.listing_items_snapshot(self.listing), [])
        self.assertEqual(self.listing.read_bytes(), original)
        # Only missing exports permit the non-mutating collection fallback.
        with Store(self.database) as store:
            from applicant.domain.job import Job

            store.save_jobs([Job(source='fixture', id='stored', title='Stored')], self.listing)
        self.listing.unlink()
        self.assertEqual(self.cli('--offline')[0], 0)
        self.assertFalse(self.listing.exists())

    def test_bundle_hashes_and_raw_json_numbers_survive_preflight(self):
        job = version('raw')
        job = JobVersion.model_validate(
            job.model_dump()
            | {
                'job_data': job.job_data | {'experience_min': 1.25},
                'evidence': (
                    Evidence(
                        source='raw',
                        observed_at=NOW,
                        extractor_version='raw',
                        payload={'number': 0.1},
                    ),
                ),
            }
        )
        path = self.bundle((job,))
        decoded = JobVersionBundle.model_validate(read_ranking_document(path)).jobs[0]
        self.assertEqual(decoded.content_hash, job.content_hash)
        run = self.service(versions_path=path)
        with Store.open_readonly(self.database) as store:
            self.assertEqual(
                load_history(store, run.ranking_run_id).jobs[0].content_hash, job.content_hash
            )

    def test_all_invalid_inputs_fail_before_writer_or_network(self):
        original = self.database.read_bytes()
        bad_jobs = [
            (version('a'), version('a')),
            (
                JobVersion.model_validate(
                    version().model_dump() | {'captured_at': NOW + timedelta(seconds=1)}
                ),
            ),
            (
                version(
                    bundles=(
                        RankingFacts(
                            compensation=(
                                compensation_facts().model_copy(
                                    update={
                                        'source': compensation_facts().source.model_copy(
                                            update={'observed_at': NOW + timedelta(seconds=1)}
                                        )
                                    }
                                ),
                            )
                        ),
                    )
                ),
            ),
            (
                JobVersion.model_validate(
                    version().model_dump()
                    | {
                        'evidence': (
                            Evidence(
                                source='x',
                                observed_at=NOW + timedelta(seconds=1),
                                extractor_version='x',
                                payload={'kind': 'ranking_facts', 'schema_version': 99},
                            ),
                        )
                    }
                ),
            ),
            (
                JobVersion.model_validate(
                    version().model_dump()
                    | {
                        'evidence': (
                            Evidence(
                                source='x',
                                observed_at=NOW,
                                extractor_version='x',
                                payload={'kind': 'ranking_facts', 'routes': 'invalid'},
                            ),
                        )
                    }
                ),
            ),
        ]
        for jobs in bad_jobs:
            with (
                self.subTest(jobs=jobs),
                mock.patch('applicant.services.ranking_cli.Store', wraps=Store) as owner,
            ):
                self.bundle(jobs)
                with self.assertRaises(ConfigError):
                    self.service(versions_path=self.root / 'versions.json')
                owner.assert_not_called()
        for data in ([], {'unknown': 'Senior'}, {' ': 'x'}, {'id': ''}, {'id': ' x'}, {'id': 1}):
            with self.subTest(tiers=data):
                self.bundle((version(),))
                self.write('tiers.json', data)
                self.assertEqual(
                    self.cli('--job-versions', 'versions.json', '--tiers', 'tiers.json')[0], 2
                )
        self.assertEqual(self.database.read_bytes(), original)

    def test_bad_documents_and_io_errors_are_explicit(self):
        for text in ('{', '[]', '{"x":NaN}', '{"x":Infinity}', '{"x":1e999}', '{"x":1,"x":2}'):
            with self.subTest(text=text):
                path = self.root / 'bad.json'
                path.write_text(text, encoding='utf-8')
                self.assertEqual(self.cli('--scoring-config', 'bad.json')[0], 2)
                self.assertEqual(self.cli('--job-versions', 'bad.json')[0], 2)
        path.write_bytes(b'\xff')
        with self.assertRaises(ConfigError):
            read_ranking_document(path)
        self.assertEqual(self.cli('--scoring-config', 'missing.json')[0], 1)
        self.assertEqual(self.cli('--provenance', 'bad.json')[0], 2)
        self.provenance.write_text('{}', encoding='utf-8')
        self.assertEqual(self.cli()[0], 2)

    def test_empty_missing_profile_backend_and_argument_errors(self):
        original = self.database.read_bytes()
        self.assertEqual(self.cli('--offline')[0], 1)
        self.assertEqual(self.cli('--profile', 'absent')[0], 2)
        self.assertEqual(self.cli('--revision', '999')[0], 2)
        self.assertEqual(self.cli('--profile', ' ')[0], 2)
        self.assertEqual(self.cli('--store', 'files')[0], 2)
        self.assertEqual(self.cli('--database', 'absent/db.sqlite')[0], 2)
        self.assertFalse((self.root / 'absent').exists())
        self.assertEqual(self.database.read_bytes(), original)
        for options in (
            ['--input', 'x', '--job-versions', 'y'],
            ['--top', '0'],
            ['--top', '1', '--all'],
            ['--force'],
            ['--region', 'Europe'],
        ):
            with (
                self.subTest(options=options),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as stopped,
            ):
                self.cli(*options)
            self.assertEqual(stopped.exception.code, 2)

    def test_custom_config_tiers_and_exact_revision_after_profile_change(self):
        job = version(
            'tier', bundles=(RankingFacts(compensation=(compensation_facts(tier='Senior'),)),)
        )
        self.bundle((job,))
        self.write('tiers.json', {job.job_id: 'Senior'})
        config = ScoringConfigurationV2(annual_hours=Decimal('2000.1234567890123456789'))
        self.write('config.json', config)
        with Store(self.database) as store:
            save_profile(
                store, candidate(profile_id='default', profile_revision=2), expected_revision=1
            )
        code, out, err = self.cli(
            '--job-versions',
            'versions.json',
            '--tiers',
            'tiers.json',
            '--scoring-config',
            'config.json',
            '--revision',
            '1',
            '--offline',
            '--json',
        )
        self.assertEqual(code, 0, err)
        run_id = json.loads(out)['run']['ranking_run_id']
        with Store.open_readonly(self.database) as store:
            history = load_history(store, run_id)
            assert isinstance(history.run, RankingRunV3)
            self.assertEqual(history.run.selected_tiers, {job.job_id: 'Senior'})
            self.assertEqual(history.run.profile_revision, 1)
            self.assertEqual(history.config.annual_hours, config.annual_hours)

    def test_input_override_does_not_change_default_database_and_explicit_database_works(self):
        self.write('other.json', {'list': [version().job_data]})
        self.assertEqual(self.cli('--input', 'other.json', '--offline')[0], 0)
        target = self.root / 'other.sqlite'
        with Store(target) as store:
            save_profile(store, self.person, expected_revision=0)
        self.assertEqual(
            self.cli('--database', str(target), '--input', 'other.json', '--offline')[0], 0
        )

    def test_read_permission_failure_is_not_treated_as_missing_profile(self):
        with (
            mock.patch.object(Path, 'stat', side_effect=PermissionError('denied')),
            self.assertRaises(StoreError),
        ):
            self.service()

    def test_head_change_after_preflight_does_not_change_frozen_revision(self):
        from applicant.services.ranking import rank_jobs

        self.bundle((version(),))
        clock = mock.Mock(return_value=NOW)

        def changed(store, **kwargs):
            save_profile(
                store, candidate(profile_id='default', profile_revision=2), expected_revision=1
            )
            return rank_jobs(store, **kwargs)

        with mock.patch('applicant.services.ranking_cli.rank_jobs', side_effect=changed):
            run = self.service(versions_path=self.root / 'versions.json', clock=clock)
        clock.assert_called_once_with()
        self.assertEqual(run.profile_revision, 1)

    def test_online_fx_failure_reports_fallback_without_hiding_jobs(self):
        from applicant.errors import Unreachable

        self.bundle((version(bundles=(RankingFacts(compensation=(compensation_facts(),)),)),))
        with mock.patch(
            'applicant.services.ranking_fx.FrankfurterFX.fetch', side_effect=Unreachable('offline')
        ) as fetch:
            code, out, err = self.cli('--job-versions', 'versions.json')
        self.assertEqual(code, 0, err)
        fetch.assert_called_once()
        self.assertIn('fx_provider_unavailable', out)
        self.assertIn('review_required: 1 displayed / 1 total', out)
