"""Historical CLI modes use immutable snapshots and explicit replay provenance."""

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from applicant.cli import main
from applicant.domain.ranking import JobRanking, RankingRun
from applicant.domain.ranking_facts import RankingFacts, WorkRoute
from applicant.domain.ranking_report import ranking_report
from applicant.domain.ranking_v2 import RankingRunV2
from applicant.errors import StoreError
from applicant.infra.store.sqlite import Store
from applicant.services.preferences import save_profile
from applicant.services.ranking_history import complete_run, load_history
from applicant.services.ranking_read import list_runs, read_run
from tests import test_ranking_cli as creation
from tests.ranking_fixtures import FIXTURES, NOW, compensation_facts
from tests.test_eligibility import candidate
from tests.test_ranking_engine import version


class HistoricalCliTest(unittest.TestCase):
    def setUp(self):
        self.data = creation.RankingCliTest()
        self.data.setUp()
        self.addCleanup(self.data.doCleanups)
        self.root = self.data.root
        self.database = self.data.database
        self.created = self.data.service(versions_path=self.data.bundle(self.data.mixed_jobs()))

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = main(['--no-log-file', '--data-dir', str(self.root), 'rank', *args])
            except SystemExit as stopped:
                code = stopped.code
        return code, out.getvalue(), err.getvalue()

    def test_display_and_listing_ignore_current_inputs_and_never_evaluate_or_acquire(self):
        expected = self.cli('show', self.created.ranking_run_id, '--json')[1]
        with Store(self.database) as writer:
            save_profile(
                writer, candidate(profile_id='default', profile_revision=2), expected_revision=1
            )
        self.data.listing.write_text('broken', encoding='utf-8')
        self.data.provenance.unlink()
        original = self.database.read_bytes()
        with (
            mock.patch('applicant.cli.rank.now', side_effect=AssertionError('clock')),
            mock.patch(
                'applicant.cli.rank.create_ranking_run', side_effect=AssertionError('create')
            ),
            mock.patch(
                'applicant.services.ranking.evaluate_jobs', side_effect=AssertionError('evaluate')
            ),
            mock.patch('httpx.Client.request', side_effect=AssertionError('network')),
        ):
            self.assertEqual(self.cli('show', self.created.ranking_run_id, '--json')[1], expected)
            code, text, _ = self.cli('show', self.created.ranking_run_id, '--all')
            self.assertEqual(code, 0)
            self.assertIn('excluded:', text)
            code, out, _ = self.cli('list', '--json')
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)['runs'][0]['profile_revision'], 1)
            self.assertIn(self.created.ranking_run_id, self.cli('list')[1])
            self.assertEqual(
                json.loads(self.cli('list', '--profile', 'absent', '--json')[1])['runs'], []
            )
        self.assertEqual(self.database.read_bytes(), original)

    def test_conflict_explanation_contains_both_sources_values_and_verification_prompt(self):
        assertions = json.loads((FIXTURES / 'route-conflict.json').read_text())['assertions']
        job = version(
            'conflict',
            bundles=tuple(
                RankingFacts(
                    routes=(WorkRoute.model_validate(a),),
                    routes_complete=True,
                    compensation=(
                        compensation_facts(
                            currency='EUR', period='annual', minimum=90000, maximum=90000
                        ),
                    ),
                )
                for a in assertions
            ),
        )
        run = self.data.service(versions_path=self.data.bundle((job,)))
        code, out, err = self.cli('explain', run.ranking_run_id, job.job_id, '--json')
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(report['kind'], 'ranking_explanation')
        self.assertEqual(report['eligibility'], 'review_required')
        criterion = next(c for c in report['result']['decision']['criteria'] if c['conflicts'])
        conflict = criterion['conflicts'][0]
        self.assertTrue(conflict['needs_verification'])
        self.assertEqual(len(conflict['assertions']), 2)
        self.assertIn('worldwide', out)
        self.assertIn('US applicants', out)
        self.assertEqual(report['profile']['profile_revision'], 1)
        self.assertEqual(report['job']['job_version_id'], run.job_version_ids[0])
        self.assertIn('fx', report)
        self.assertIn('weights', report['profile'])
        code, text, _ = self.cli('explain', run.ranking_run_id, job.job_id)
        self.assertEqual(code, 0)
        self.assertIn('needs_verification', text)
        self.assertIn('US applicants', text)
        self.assertEqual(self.cli('explain', run.ranking_run_id, 'not-member')[0], 4)

    def test_replay_verification_and_build_mismatch_are_explicit_and_readonly(self):
        original = self.database.read_bytes()
        args = ['replay', self.created.ranking_run_id, '--provenance', 'provenance.json']
        code, out, err = self.cli(*args, '--json')
        self.assertEqual(code, 0, err)
        self.assertTrue(json.loads(out)['verification']['matches'])
        self.assertIn('matches', self.cli(*args)[1])
        self.data.write(
            'wrong.json', json.loads(self.data.provenance.read_text()) | {'build_identity': 'other'}
        )
        self.assertEqual(
            self.cli('replay', self.created.ranking_run_id, '--provenance', 'wrong.json')[0], 1
        )
        self.data.write('bad.json', {})
        self.assertEqual(
            self.cli('replay', self.created.ranking_run_id, '--provenance', 'bad.json')[0], 2
        )
        with mock.patch(
            'applicant.services.ranking_replay.evaluate_jobs',
            return_value=tuple(
                r.model_copy(
                    update={
                        'decision': r.decision.model_copy(
                            update={
                                'criteria': (
                                    r.decision.criteria[0].model_copy(
                                        update={'explanation': 'changed explanation'}
                                    ),
                                    *r.decision.criteria[1:],
                                )
                            }
                        )
                    }
                )
                for r in self.created.results
            ),
        ):
            code, text, _ = self.cli(*args)
            self.assertEqual(code, 1)
            self.assertIn('mismatched job version', text)
        self.assertEqual(self.database.read_bytes(), original)

    def test_legacy_and_v2_runs_display_without_modern_recalculation(self):
        first = self.created.results[0]
        legacy = RankingRun(
            ranking_run_id='legacy',
            profile_id=self.created.profile_id,
            profile_revision=1,
            scoring_config_id=self.created.scoring_config_id,
            fx_snapshot_id=self.created.fx_snapshot_id,
            algorithm_version='supplied',
            build_identity='legacy',
            lockfile_digest='a' * 64,
            calculated_at=NOW,
            job_version_ids=self.created.job_version_ids,
            results=(
                JobRanking(
                    job_id=first.job_id, job_version_id=first.job_version_id, eligibility='eligible'
                ),
            ),
        )
        v2 = RankingRunV2.model_validate(
            self.created.model_dump(exclude={'selected_tiers', 'engine_policy'})
            | {'schema_version': 2, 'ranking_run_id': 'v2'}
        )
        with Store(self.database) as store:
            complete_run(store, legacy)
            complete_run(store, v2)
        code, out, _ = self.cli('show', 'legacy', '--json', '--top', '1')
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertNotIn('groups', report)
        self.assertEqual(report['legacy']['displayed'], 1)
        self.assertEqual(
            report['legacy']['results'][0]['job_version_id'], self.created.job_version_ids[0]
        )
        report = json.loads(self.cli('show', 'legacy', '--all', '--json')[1])
        self.assertIsNone(report['legacy']['results'][-1]['ranking'])
        self.assertIn('eligibility unavailable in this format', self.cli('show', 'legacy')[1])
        no_result_id = self.data.mixed_jobs()[-1].job_id
        explanation = json.loads(self.cli('explain', 'legacy', no_result_id, '--json')[1])
        self.assertIsNone(explanation['result'])
        self.assertIsNone(explanation['eligibility'])
        self.assertIn('format_notice', explanation)
        self.assertEqual(self.cli('show', 'v2', '--json')[0], 0)
        for key in ('legacy', 'v2'):
            self.assertEqual(self.cli('replay', key, '--provenance', 'provenance.json')[0], 1)

    def test_missing_database_modes_and_missing_runs_create_no_files(self):
        missing = self.root / 'missing' / 'applicant.db'
        for mode in (
            ['show', 'absent'],
            ['replay', 'absent', '--provenance', 'provenance.json'],
        ):
            self.assertEqual(self.cli(*mode, '--database', str(missing))[0], 4)
        code, out, _ = self.cli('list', '--database', str(missing), '--json')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['runs'], [])
        self.assertIn('no completed', self.cli('list', '--database', str(missing))[1])
        self.assertFalse(missing.parent.exists())
        self.assertEqual(self.cli('show', 'absent')[0], 4)
        self.assertEqual(self.cli('replay', 'absent', '--provenance', 'provenance.json')[0], 4)

    def test_invalid_combinations_are_usage_errors_before_reading_history(self):
        run = self.created.ranking_run_id
        options = [
            [],
            ['replay', run],
            ['--explain', 'job'],
            ['--profile', ' '],
            ['show', ' '],
            ['replay', ' '],
            ['explain', run, ' '],
        ]
        for extra in (
            ['--provenance', 'provenance.json'],
            ['--profile', 'default'],
            ['--revision', '1'],
            ['--input', 'x'],
            ['--job-versions', 'x'],
            ['--scoring-config', 'x'],
            ['--tiers', 'x'],
            ['--offline'],
        ):
            options.append(['show', run, *extra])
        for mode in (['replay', run, '--provenance', 'provenance.json'], ['list']):
            for extra in (['--top', '1'], ['--all'], ['--explain', 'job'], ['--offline']):
                options.append([*mode, *extra])
        options.extend(
            [
                ['replay', run, '--provenance', 'provenance.json', '--profile', 'default'],
                ['list', '--provenance', 'provenance.json'],
                ['explain', run, 'job', '--all'],
                ['explain', run, 'job', '--top', '1'],
            ]
        )
        for args in options:
            with self.subTest(args=args):
                self.assertEqual(self.cli(*args)[0], 2)

    def test_permission_and_corruption_remain_storage_errors(self):
        with (
            mock.patch.object(Path, 'stat', side_effect=PermissionError('denied')),
            self.assertRaises(StoreError),
        ):
            list_runs(self.database)
        with Store(self.database) as store, store.db:
            store.db.execute('DROP TRIGGER immutable_ranking_runs_update')
            store.db.execute(
                "UPDATE ranking_runs SET payload = '{}' WHERE ranking_run_id = ?",
                (self.created.ranking_run_id,),
            )
        self.assertEqual(self.cli('list')[0], 1)
        self.assertEqual(self.cli('show', self.created.ranking_run_id)[0], 1)

    def test_corrupt_engine_config_is_not_treated_as_a_legacy_report(self):
        from applicant.domain.ranking import ScoringConfiguration

        with Store.open_readonly(self.database) as store:
            history = load_history(store, self.created.ranking_run_id)
        with self.assertRaises(ValueError):
            ranking_report(self.created, history.jobs, config=ScoringConfiguration())
        with (
            mock.patch(
                'applicant.services.ranking_read.ranking_report',
                side_effect=ValueError('bad config'),
            ),
            self.assertRaises(StoreError),
        ):
            read_run(self.database, self.created.ranking_run_id)
