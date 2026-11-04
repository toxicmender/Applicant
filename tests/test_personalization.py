"""Bounded weight previews and atomic, idempotent acceptance."""

import unittest
from decimal import Decimal
from unittest import mock

from applicant.domain.canonical import canonical_json
from applicant.domain.feedback import FeedbackDocument, FeedbackInput
from applicant.domain.personalization import WeightSuggestion
from applicant.domain.ranking import RankingRun
from applicant.errors import StoreError
from applicant.infra.store.personalization import SuggestionRepository
from applicant.infra.store.profiles import ProfileRepository, RevisionConflict
from applicant.infra.store.sqlite import Store
from applicant.services.feedback import import_feedback
from applicant.services.personalization import accept_suggestion, show_suggestion, suggest_weights
from applicant.services.preferences import save_profile
from applicant.services.ranking_history import complete_run
from tests import test_evaluation as evaluation
from tests.ranking_fixtures import NOW


class PersonalizationTest(unittest.TestCase):
    def setUp(self):
        self.data = evaluation.EvaluationTest()
        self.data.setUp()
        self.addCleanup(self.data.doCleanups)
        self.path = self.data.path
        with Store(self.path) as store:
            save_profile(
                store,
                self.data.data.person.model_copy(
                    update={'profile_revision': 2, 'preferred_work_modes': ('Remote',)}
                ),
                expected_revision=1,
            )
        self.created = self.data.data.service(versions_path=self.data.data.bundle(self.data.jobs))

    def feedback(self, revision=0):
        events = tuple(
            FeedbackInput(
                event_id=f'weight-{i}-{revision}',
                run_id=self.created.ranking_run_id,
                job_id=result.job_id,
                expected_revision=revision,
                decision='shortlist',
                importance={
                    'compensation': 'more_important',
                    'remote_compatibility': 'less_important',
                },
            )
            for i, result in enumerate(self.created.results)
        )
        return import_feedback(self.path, FeedbackDocument(events=events), recorded_at=NOW)

    def test_insufficient_support_and_readonly_previews(self):
        before = self.path.read_bytes()
        report = suggest_weights(self.path, self.created.ranking_run_id, created_at=NOW)
        self.assertEqual(report['status'], 'insufficient_feedback')
        self.assertEqual(self.path.read_bytes(), before)
        self.feedback()
        with (
            mock.patch(
                'applicant.services.ranking.evaluate_jobs', side_effect=AssertionError('evaluation')
            ),
            mock.patch('httpx.Client.request', side_effect=AssertionError('network')),
        ):
            report = suggest_weights(self.path, self.created.ranking_run_id, created_at=NOW)
        self.assertEqual(report['status'], 'proposed')
        suggestion = show_suggestion(self.path, report['suggestion']['suggestion_id'])
        self.assertEqual(suggestion.transfer, Decimal('.05'))
        self.assertEqual(sum(suggestion.proposed_profile.weights.values()), 1)
        self.assertEqual(len(suggestion.preview), 3)
        self.assertEqual(suggestion.recipient, 'compensation')
        self.assertEqual(suggestion.donor, 'remote_compatibility')
        saved = accept_suggestion(self.path, suggestion.suggestion_id, accepted_at=NOW)
        self.assertEqual(saved.profile_revision, 3)
        self.assertEqual(
            accept_suggestion(self.path, suggestion.suggestion_id, accepted_at=NOW), saved
        )
        with Store(self.path) as store:
            self.assertEqual(ProfileRepository(store.db).get(saved.profile_id), saved)
            self.assertEqual(
                ProfileRepository(store.db).get(saved.profile_id, 1).weights,
                self.data.data.person.weights,
            )
        with self.assertRaises(RevisionConflict):
            suggest_weights(self.path, self.created.ranking_run_id, created_at=NOW)

    def test_stale_feedback_and_atomic_failure(self):
        self.feedback()
        proposal = suggest_weights(self.path, self.created.ranking_run_id, created_at=NOW)[
            'suggestion'
        ]
        self.feedback(1)
        with self.assertRaises(RevisionConflict):
            accept_suggestion(self.path, proposal['suggestion_id'], accepted_at=NOW)
        fresh = suggest_weights(self.path, self.created.ranking_run_id, created_at=NOW)[
            'suggestion'
        ]
        with (
            mock.patch(
                'applicant.services.personalization.ProfileRepository.append',
                side_effect=RuntimeError('failed'),
            ),
            self.assertRaises(RuntimeError),
        ):
            accept_suggestion(self.path, fresh['suggestion_id'], accepted_at=NOW)
        with Store(self.path) as store:
            self.assertEqual(ProfileRepository(store.db).get('default').profile_revision, 2)
            self.assertEqual(
                store.db.execute('SELECT count(*) FROM suggestion_acceptances').fetchone()[0], 0
            )
            with self.assertRaises(StoreError):
                SuggestionRepository(store.db).put(
                    show_suggestion(self.path, fresh['suggestion_id'])
                )

    def test_profile_race_cannot_persist_a_stale_suggestion(self):
        self.feedback()
        from applicant.domain.personalization import propose_weights

        def concurrent_edit(*args, **kwargs):
            result = propose_weights(*args, **kwargs)
            with Store(self.path) as store:
                save_profile(
                    store,
                    args[0].profile.model_copy(update={'profile_revision': 3}),
                    expected_revision=2,
                )
            return result

        with (
            mock.patch(
                'applicant.services.personalization.propose_weights', side_effect=concurrent_edit
            ),
            self.assertRaisesRegex(RevisionConflict, 'profile changed'),
        ):
            suggest_weights(self.path, self.created.ranking_run_id, created_at=NOW)
        with Store.open_readonly(self.path) as store:
            self.assertEqual(
                store.db.execute('SELECT count(*) FROM weight_suggestions').fetchone()[0], 0
            )

    def test_legacy_run_cannot_generate_modern_suggestions(self):
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
            | {'ranking_run_id': 'legacy-personalization', 'results': (), 'schema_version': 1}
        )
        with Store(self.path) as store:
            complete_run(store, legacy)
        with self.assertRaisesRegex(ValueError, 'modern engine run'):
            suggest_weights(self.path, legacy.ranking_run_id, created_at=NOW)

    def test_unknown_dimension_importance_does_not_supply_support(self):
        requests = tuple(
            FeedbackInput(
                event_id=f'unknown-{i}',
                run_id=self.created.ranking_run_id,
                job_id=r.job_id,
                expected_revision=0,
                decision='shortlist',
                importance={'career_growth': 'more_important'},
            )
            for i, r in enumerate(self.created.results)
        )
        import_feedback(self.path, FeedbackDocument(events=requests), recorded_at=NOW)
        self.assertEqual(
            suggest_weights(self.path, self.created.ranking_run_id, created_at=NOW)['status'],
            'insufficient_feedback',
        )

    def test_corrupt_acceptance_link_cannot_return_an_unrelated_revision(self):
        self.feedback()
        proposal = suggest_weights(self.path, self.created.ranking_run_id, created_at=NOW)[
            'suggestion'
        ]
        accept_suggestion(self.path, proposal['suggestion_id'], accepted_at=NOW)
        with Store(self.path) as store:
            store.db.execute('DROP TRIGGER immutable_suggestion_acceptances_update')
            store.db.execute('UPDATE suggestion_acceptances SET profile_revision=1')
            store.db.commit()
        with self.assertRaisesRegex(StoreError, 'acceptance does not match'):
            accept_suggestion(self.path, proposal['suggestion_id'], accepted_at=NOW)

    def test_cli_explicit_modes_and_missing_suggestions(self):
        self.assertEqual(
            self.data.cli('personalize', 'suggest', '--run', self.created.ranking_run_id)[0], 0
        )
        for action in ('show', 'accept'):
            self.assertEqual(self.data.cli('personalize', action, '--suggestion', 'missing')[0], 4)
        self.assertEqual(self.data.cli('personalize', 'suggest')[0], 2)
        self.assertEqual(self.data.cli('personalize', 'show', '--run', 'x')[0], 2)
        self.feedback()
        self.assertEqual(
            self.data.cli('personalize', 'suggest', '--run', self.created.ranking_run_id)[0], 0
        )
        proposal = suggest_weights(self.path, self.created.ranking_run_id, created_at=NOW)[
            'suggestion'
        ]
        self.assertEqual(
            self.data.cli('personalize', 'show', '--suggestion', proposal['suggestion_id'])[0], 0
        )
        self.assertEqual(
            self.data.cli('personalize', 'accept', '--suggestion', proposal['suggestion_id'])[0], 0
        )

    def test_ignored_unknown_feedback_excluded_preview_and_corruption_guards(self):
        jobs = (*self.data.jobs, *self.data.data.mixed_jobs()[1:])
        self.created = self.data.data.service(versions_path=self.data.data.bundle(jobs))
        events = tuple(
            FeedbackInput(
                event_id=f'complex-{i}',
                run_id=self.created.ranking_run_id,
                job_id=result.job_id,
                expected_revision=0,
                decision='shortlist',
                importance={
                    'compensation': 'more_important',
                    'remote_compatibility': 'less_important',
                    'career_growth': 'more_important',
                },
            )
            for i, result in enumerate(self.created.results)
        )
        import_feedback(self.path, FeedbackDocument(events=events), recorded_at=NOW)
        proposal = suggest_weights(self.path, self.created.ranking_run_id, created_at=NOW)[
            'suggestion'
        ]
        self.assertEqual(len(proposal['preview_groups']['excluded']), 1)
        with self.assertRaises(ValueError):
            WeightSuggestion.model_validate(proposal | {'donor': proposal['recipient']})
        with Store(self.path) as store:
            store.db.execute('DROP TRIGGER immutable_weight_suggestions_update')
            store.db.execute(
                'UPDATE weight_suggestions SET payload=? WHERE suggestion_id=?',
                (
                    canonical_json(
                        proposal
                        | {
                            'profile_id': 'wrong',
                            'proposed_profile': dict(proposal['proposed_profile'])
                            | {'profile_id': 'wrong'},
                        }
                    ),
                    proposal['suggestion_id'],
                ),
            )
            store.db.commit()
            with self.assertRaisesRegex(StoreError, 'identity'):
                SuggestionRepository(store.db).get(proposal['suggestion_id'])
