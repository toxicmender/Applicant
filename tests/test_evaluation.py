"""Frozen feedback evaluation and comparisons preserve uncertainty and history."""

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from unittest import mock

from applicant.cli import main
from applicant.domain.evaluation import evaluate_feedback
from applicant.domain.feedback import FeedbackDocument, FeedbackInput
from applicant.domain.ranking import RankingRun
from applicant.domain.ranking_v2 import ScoringConfigurationV2
from applicant.infra.store.sqlite import Store
from applicant.services.evaluation import compare_history, evaluate_run
from applicant.services.feedback import import_feedback
from applicant.services.preferences import save_profile
from applicant.services.ranking_history import complete_run, load_history
from tests import test_ranking_cli as creation
from tests.ranking_fixtures import NOW


class EvaluationTest(unittest.TestCase):
    def setUp(self):
        self.data = creation.RankingCliTest()
        self.data.setUp()
        self.addCleanup(self.data.doCleanups)
        first = self.data.mixed_jobs()[0]
        self.jobs = tuple(
            type(first).model_validate(
                first.model_dump()
                | {'job_version_id': f'job-{i}', 'job_data': first.job_data | {'id': str(i)}}
            )
            for i in range(3)
        )
        self.created = self.data.service(versions_path=self.data.bundle(self.jobs))
        self.path = self.data.database

    def save(self, index, decision, revision=0):
        return import_feedback(
            self.path,
            FeedbackDocument(
                events=(
                    FeedbackInput(
                        event_id=f'{index}-{revision}',
                        run_id=self.created.ranking_run_id,
                        job_id=self.created.results[index].job_id,
                        expected_revision=revision,
                        decision=decision,
                        reasons=('other',) if decision == 'dismiss' else (),
                    ),
                )
            ),
            recorded_at=NOW,
        )[0]

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(['--no-log-file', '--data-dir', str(self.data.root), *args])
        return code, out.getvalue(), err.getvalue()

    def test_partial_full_clear_and_frozen_metrics(self):
        empty = evaluate_run(self.path, self.created.ranking_run_id)
        self.assertIsNone(empty['metrics']['precision_at_k'])
        self.assertEqual(empty['metrics']['precision_upper_bound'], 1)
        self.save(0, 'shortlist')
        partial = evaluate_run(self.path, self.created.ranking_run_id)
        self.assertEqual(
            partial['metrics']['precision_lower_bound'],
            Decimal('0.3333333333333333333333333333333333'),
        )
        self.assertIsNone(partial['metrics']['recall_at_k'])
        self.save(1, 'dismiss')
        self.save(2, 'shortlist')
        full = evaluate_run(self.path, self.created.ranking_run_id)
        self.assertEqual(
            full['metrics']['precision_at_k'], Decimal('0.6666666666666666666666666666666667')
        )
        self.assertEqual(full['metrics']['recall_at_k'], 1)
        self.assertEqual(
            evaluate_run(
                self.path, self.created.ranking_run_id, through=partial['feedback_through']
            ),
            partial,
        )
        self.save(0, 'clear', 1)
        self.assertIsNone(
            evaluate_run(self.path, self.created.ranking_run_id)['metrics']['precision_at_k']
        )
        with self.assertRaises(ValueError):
            evaluate_run(self.path, self.created.ranking_run_id, through=999)
        with self.assertRaises(ValueError):
            evaluate_run(self.path, self.created.ranking_run_id, through=-1)

    def test_empty_eligible_and_undefined_recall(self):
        mixed = self.data.service(versions_path=self.data.bundle(self.data.mixed_jobs()))
        with Store(self.path) as store:
            history = load_history(store, mixed.ranking_run_id)
        assert not isinstance(history.run, RankingRun)
        assert isinstance(history.config, ScoringConfigurationV2)
        result = evaluate_feedback(history.run, history.config, (), top=1, through=0)
        self.assertEqual(result['other_groups']['excluded']['candidates'], 1)
        run = self.created.model_copy(update={'job_version_ids': (), 'results': ()})
        empty = evaluate_feedback(run, ScoringConfigurationV2(), (), through=0)
        self.assertTrue(all(v is None for v in empty['metrics'].values()))
        self.save(0, 'dismiss')
        self.save(1, 'dismiss')
        self.save(2, 'dismiss')
        self.assertIsNone(
            evaluate_run(self.path, self.created.ranking_run_id)['metrics']['recall_at_k']
        )
        for top, through in ((0, 0), (1, -1)):
            with self.assertRaises(ValueError):
                evaluate_feedback(
                    self.created, ScoringConfigurationV2(), (), top=top, through=through
                )
        event = self.save(0, 'shortlist', 1)
        with self.assertRaises(ValueError):
            evaluate_feedback(self.created, ScoringConfigurationV2(), (event,), through=0)

    def test_compare_changed_added_removed_and_legacy(self):
        changed = type(self.jobs[0]).model_validate(
            self.jobs[0].model_dump()
            | {
                'job_version_id': 'changed',
                'job_data': self.jobs[0].job_data | {'salary': 'changed'},
            }
        )
        extra = type(self.jobs[1]).model_validate(
            self.jobs[1].model_dump()
            | {'job_version_id': 'extra', 'job_data': self.jobs[1].job_data | {'id': 'extra'}}
        )
        other = self.data.service(versions_path=self.data.bundle((changed, self.jobs[1], extra)))
        report = compare_history(self.path, self.created.ranking_run_id, other.ranking_run_id)
        self.assertCountEqual(
            [r['membership'] for r in report['jobs']],
            ('changed_version', 'unchanged_version', 'added', 'removed'),
        )
        self.assertEqual(
            next(r for r in report['jobs'] if r['membership'] == 'unchanged_version')['changes'][
                'overall_score'
            ]['delta'],
            0,
        )
        legacy = RankingRun.model_validate(
            self.created.model_dump(
                exclude={
                    'selected_tiers',
                    'engine_policy',
                    'results',
                    'schema_version',
                    'resolution_diagnostics',
                }
            )
            | {'ranking_run_id': 'legacy', 'results': (), 'schema_version': 1}
        )
        with Store(self.path) as store:
            complete_run(store, legacy)
        compared = compare_history(self.path, legacy.ranking_run_id, self.created.ranking_run_id)
        self.assertTrue(all(r['base_position'] is None for r in compared['jobs']))
        self.assertTrue(all(r['membership'] == 'unchanged_version' for r in compared['jobs']))
        with self.assertRaises(ValueError):
            evaluate_run(self.path, legacy.ranking_run_id)
        person = self.data.person.model_copy(update={'profile_id': 'another'})
        with Store(self.path) as store:
            save_profile(store, person, expected_revision=0)
            complete_run(
                store,
                legacy.model_copy(update={'ranking_run_id': 'another', 'profile_id': 'another'}),
            )
        with self.assertRaises(ValueError):
            compare_history(self.path, 'legacy', 'another')

    def test_cli_exports_readonly_and_rejects_mixed_modes(self):
        self.save(0, 'shortlist')
        before = self.path.read_bytes()
        with mock.patch('httpx.Client.request', side_effect=AssertionError('network')):
            code, plain, _ = self.cli('evaluate', '--run', self.created.ranking_run_id)
            self.assertEqual(code, 0)
            code, out, _ = self.cli(
                'evaluate', '--run', self.created.ranking_run_id, '--export', 'evaluation.json'
            )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(plain), json.loads(out))
            self.assertEqual(
                json.loads(out), json.loads((self.data.root / 'evaluation.json').read_text())
            )
            code, out, _ = self.cli(
                'rank',
                '--compare',
                self.created.ranking_run_id,
                self.created.ranking_run_id,
                '--json',
            )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)['changed_run_inputs'], [])
            self.assertEqual(
                self.cli(
                    'rank', '--compare', self.created.ranking_run_id, self.created.ranking_run_id
                )[0],
                0,
            )
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.cli('evaluate', '--run', 'missing')[0], 4)
        self.assertEqual(self.cli('evaluate', '--run', ' ')[0], 2)
        for flag, value in (('--top', '0'), ('--feedback-through', '-1')):
            self.assertEqual(self.cli('evaluate', '--run', 'missing', flag, value)[0], 2)
        self.assertEqual(
            self.cli('evaluate', '--run', self.created.ranking_run_id, '--export', str(self.path))[
                0
            ],
            2,
        )
        self.assertEqual(
            self.cli(
                'rank',
                '--compare',
                self.created.ranking_run_id,
                self.created.ranking_run_id,
                '--top',
                '1',
            )[0],
            2,
        )
        self.assertEqual(self.cli('rank', '--compare', 'missing', 'missing')[0], 4)
        with mock.patch('applicant.cli.evaluate.write_document', side_effect=OSError('disk')):
            self.assertEqual(
                self.cli('evaluate', '--run', self.created.ranking_run_id, '--export', 'x.json')[0],
                1,
            )
