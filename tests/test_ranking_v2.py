"""Engine boundaries: unknowns, conflicts, history compatibility and policy identity."""

import hashlib
import unittest
from decimal import Decimal

from pydantic import ValidationError

from applicant.domain.canonical import canonical_json
from applicant.domain.preferences import CandidateProfile
from applicant.domain.ranking import ScoringConfiguration
from applicant.domain.ranking_facts import CompensationFacts, FactSource, RankingFacts, WorkRoute
from applicant.domain.ranking_v2 import (
    CandidateProfileV2,
    ConflictAssertion,
    CriterionResult,
    DimensionResult,
    EligibilityDecision,
    EvidenceConflict,
    FXSnapshotV2,
    JobRankingV2,
    RankingRunV2,
    ScoringConfigurationV2,
    upgrade_config,
    upgrade_profile,
)
from tests.ranking_fixtures import NOW, compensation_fixture, unknown_result


class EngineContractTest(unittest.TestCase):
    def test_legacy_profile_upgrade_is_explicit_and_preserves_soft_preferences(self):
        old = CandidateProfile(
            profile_id='candidate',
            experience_years=Decimal(5),
            primary_discipline='software',
            preferred_work_modes=('Remote',),
        )
        before = canonical_json(old)
        new = upgrade_profile(old, updated_at=NOW)
        self.assertEqual(new.profile_revision, 2)
        self.assertEqual(new.preferred_work_modes, ('Remote',))
        self.assertEqual(new.required_work_modes, ())
        self.assertIsNone(new.work_country)
        self.assertEqual(canonical_json(old), before)
        self.assertEqual(CandidateProfile.model_validate_json(before), old)
        with self.assertRaises(ValidationError):
            CandidateProfile.model_validate_json(canonical_json(new))

    def test_country_registry_and_canonical_country_sets(self):
        fixture = compensation_fixture()
        for country in ('us', 'USA', 'ZZ', ''):
            with self.subTest(country=country), self.assertRaises(ValidationError):
                CandidateProfileV2(
                    profile_id='p',
                    experience_years=Decimal(1),
                    primary_discipline='s',
                    work_country=country,
                )
        route = WorkRoute(
            route_id='r',
            source=FactSource(**fixture['source']),
            mode='Remote',
            remote_scope='country_list',
            allowed_countries=('US', 'IN', 'US'),
        )
        self.assertEqual(route.allowed_countries, ('IN', 'US'))
        self.assertEqual(WorkRoute.model_validate_json(canonical_json(route)), route)
        for changes in (
            {'allowed_countries': ()},
            {'remote_scope': 'worldwide'},
            {'mode': 'Onsite'},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                WorkRoute.model_validate(route.model_dump() | changes)

    def test_compensation_golden_facts_remain_separate_and_round_trip(self):
        fixture = compensation_fixture()
        values = tuple(
            CompensationFacts(source=FactSource(**fixture['source']), **case['facts'])
            for case in fixture['cases']
        )
        facts = RankingFacts(compensation=values)
        self.assertEqual(RankingFacts.model_validate_json(canonical_json(facts)), facts)
        self.assertIsNone(values[2].minimum)
        self.assertEqual(values[3].tier, 'Senior')
        self.assertEqual(values[4].tier, 'Staff')
        self.assertNotEqual(values[3].maximum, values[4].maximum)
        fixture['source']['source'] = 'mutated'
        self.assertEqual(compensation_fixture()['source']['source'], 'fixture-employer')

    def test_invalid_salary_shapes_and_nonfinite_values_are_rejected(self):
        source = compensation_fixture()['source']
        for fields in (
            {'shape': 'exact', 'minimum': 1, 'maximum': 2},
            {'shape': 'range', 'minimum': 2, 'maximum': 1},
            {'shape': 'lower_only', 'maximum': 10},
            {'shape': 'unknown', 'minimum': 0},
            {'shape': 'exact', 'minimum': 'NaN', 'maximum': 'NaN'},
        ):
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                CompensationFacts.model_validate(
                    {'source': source, 'original_text': 'pay'} | fields
                )

    def test_configuration_identity_includes_behavior_but_not_record_metadata(self):
        config = ScoringConfigurationV2(created_at=NOW)
        self.assertEqual(
            config.config_hash,
            '7e5c84774d024b1ea689e97bd0aae7d378fafb79e6df531f3577885453053370',
        )
        self.assertEqual(config.config_hash, ScoringConfigurationV2(created_at=NOW).config_hash)
        changes = (
            {'annual_hours': '2000'},
            {'fx_max_age_days': 3},
            {'review_count_saturation': 40},
            {'source_priority': ('ambitionbox',)},
            {'technology_aliases': {}},
            {'allow_static_fx': True},
        )
        for change in changes:
            with self.subTest(change=change):
                changed = ScoringConfigurationV2.model_validate(config.model_dump() | change)
                self.assertNotEqual(config.config_hash, changed.config_hash)
        old = ScoringConfiguration(annual_hours=Decimal(1800))
        new = upgrade_config(old, created_at=NOW)
        self.assertEqual(new.annual_hours, 1800)
        self.assertNotEqual(new.scoring_config_id, old.scoring_config_id)
        self.assertNotEqual(new.config_hash, old.config_hash)

    def test_invalid_policy_parameters(self):
        base = ScoringConfigurationV2().model_dump()
        for change in (
            {'evidence_weights': base['evidence_weights'] | {'unknown': '.5'}},
            {'source_priority': ('glassdoor', 'glassdoor')},
            {'technology_aliases': {'py': 'python', 'python': 'snake'}},
            {'technology_aliases': {'JS': 'javascript'}},
            {'category_dimensions': {'overall': 'technical_fit'}},
            {'fx_max_age_days': -1},
            {'review_count_saturation': 0},
            {'arithmetic_policy': 'ambient'},
            {'schema_version': 3},
        ):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                ScoringConfigurationV2.model_validate(base | change)

    def test_conflicts_retain_display_details_and_aggregate_as_review(self):
        assertions = tuple(
            ConflictAssertion(
                evidence_id=str(i),
                source=source,
                observed_at=NOW,
                normalized_value=value,
                excerpt=excerpt,
            )
            for i, (source, value, excerpt) in enumerate(
                (
                    ('structured', 'US', 'US applicants only'),
                    ('description', 'worldwide', 'Work from anywhere'),
                )
            )
        )
        conflict = EvidenceConflict(
            criterion='remote_location',
            route_id='remote',
            assertions=assertions,
            explanation='US-only and worldwide statements conflict.',
            needs_verification='Confirm permitted work countries.',
        )
        criterion = CriterionResult(
            criterion='remote_location',
            status='unknown',
            reason_code=conflict.code,
            explanation=conflict.explanation,
            conflicts=(conflict,),
        )
        decision = EligibilityDecision(criteria=(criterion,))
        self.assertEqual(decision.status, 'review_required')
        loaded = EligibilityDecision.model_validate_json(canonical_json(decision))
        self.assertEqual(
            loaded.criteria[0].conflicts[0].assertions[1].excerpt, 'Work from anywhere'
        )
        with self.assertRaises(ValidationError):
            CriterionResult.model_validate(criterion.model_dump() | {'status': 'pass'})
        with self.assertRaises(ValidationError):
            EvidenceConflict.model_validate(
                conflict.model_dump() | {'assertions': (assertions[0],) * 2}
            )
        failure = CriterionResult(
            criterion='employment',
            status='fail',
            reason_code='mismatch',
            explanation='Contract only.',
        )
        self.assertEqual(EligibilityDecision(criteria=(criterion, failure)).status, 'excluded')

    def test_unknown_and_known_zero_are_distinct(self):
        result = unknown_result()
        self.assertIsNone(result.overall_score)
        self.assertEqual(result.confidence, 0)
        known_zero = DimensionResult(
            dimension='compensation', status='known', score=Decimal(0), quality=Decimal(1)
        )
        scored = JobRankingV2.model_validate(
            result.model_dump()
            | {
                'dimensions': (known_zero, *result.dimensions[1:]),
                'overall_score': 0,
                'confidence': '.25',
                'coverage': '.25',
            }
        )
        self.assertEqual(scored.overall_score, 0)
        for change in (
            {'score': 0},
            {'quality': '.5'},
            {'calculation_terms': {'x': Decimal('NaN')}},
        ):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                DimensionResult.model_validate(result.dimensions[0].model_dump() | change)
        with self.assertRaises(ValidationError):
            JobRankingV2.model_validate(scored.model_dump() | {'confidence': 1})

    def test_engine_runs_require_complete_ordered_results(self):
        run = RankingRunV2(
            profile_id='p',
            profile_revision=1,
            fx_snapshot_id='fx',
            scoring_config_id='config',
            build_identity='fixture',
            lockfile_digest='a' * 64,
            calculated_at=NOW,
            job_version_ids=('version',),
            results=(unknown_result(),),
        )
        self.assertEqual(RankingRunV2.model_validate_json(canonical_json(run)), run)
        for change in (
            {'results': ()},
            {'job_version_ids': ('version', 'version')},
            {'job_version_ids': ()},
        ):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                RankingRunV2.model_validate(run.model_dump() | change)

    def test_live_fx_requires_and_checks_acquisition_provenance(self):
        response = '[{"base":"EUR","quote":"USD","rate":1.25}]'
        fields = {
            'snapshot_id': 'fx',
            'base_currency': 'EUR',
            'source': 'frankfurter',
            'captured_at': NOW,
            'retrieved_at': NOW,
            'effective_date': NOW.date(),
            'provenance': 'live',
            'rates': {'EUR': 1, 'USD': '1.25'},
            'request_url': 'https://api.frankfurter.dev/v2/providers/ECB/rates',
            'provider': 'ECB',
            'raw_response': response,
            'response_digest': hashlib.sha256(response.encode()).hexdigest(),
        }
        value = FXSnapshotV2.model_validate(fields)
        self.assertEqual(FXSnapshotV2.model_validate_json(canonical_json(value)), value)
        for change in (
            {'raw_response': None},
            {'response_digest': '0' * 64},
            {'provider': None},
            {'parent_snapshot_id': 'fx'},
        ):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                FXSnapshotV2.model_validate(fields | change)
