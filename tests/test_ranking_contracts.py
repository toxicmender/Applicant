"""Validation and canonical identity of durable ranking inputs."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from pydantic import ValidationError

from applicant.domain.canonical import canonical_json, content_hash
from applicant.domain.compensation import NormalizedCompensation
from applicant.domain.fx import FXSnapshot
from applicant.domain.job import Job
from applicant.domain.preferences import CandidateProfile, default_weights
from applicant.domain.ranking import JobVersion, ScoringConfiguration

NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)


def profile(**changes):
    return CandidateProfile(
        **(
            {'profile_id': 'candidate', 'experience_years': 5, 'primary_discipline': 'software'}
            | changes
        )
    )


def fx(**changes):
    data = {
        'snapshot_id': 'fx',
        'base_currency': 'EUR',
        'source': 'fixture',
        'captured_at': NOW,
        'retrieved_at': NOW,
        'effective_date': NOW.date(),
        'provenance': 'cache',
        'rates': {'EUR': '1', 'INR': '90'},
    }
    return FXSnapshot(**(data | changes))


class ContractTest(unittest.TestCase):
    def test_canonical_golden_and_decimal_equivalence(self):
        self.assertEqual(
            canonical_json({'z': Decimal('1.00'), 'a': NOW}),
            '{"a":"2026-11-02T00:00:00.000000Z","z":"1"}',
        )
        self.assertEqual(
            content_hash({'a': 1}),
            '015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862',
        )
        self.assertEqual(content_hash({'a': Decimal('1.0')}), content_hash({'a': Decimal('1.00')}))
        with self.assertRaises(ValueError):
            canonical_json(float('nan'))

    def test_profile_roundtrip_and_zero_weights(self):
        weights = dict.fromkeys(default_weights(), Decimal(0))
        weights['technical_fit'] = Decimal(1)
        value = profile(weights=weights)
        self.assertEqual(CandidateProfile.model_validate_json(canonical_json(value)), value)
        self.assertEqual(value.salary_basis, 'gross')
        weights['technical_fit'] = Decimal(0)
        self.assertEqual(value.weights['technical_fit'], 1)

    def test_profile_invalid_inputs(self):
        for changes in (
            {'salary_currency': 'ZZZ'},
            {'salary_currency': 'usd'},
            {'profile_revision': 0},
            {'schema_version': 2},
            {'updated_at': NOW.replace(tzinfo=None)},
            {'experience_years': 'NaN'},
            {'weights': {}},
            {'weights': dict.fromkeys(default_weights(), 0)},
            {'minimum_base_salary': 2, 'preferred_base_salary': 1},
            {'unknown': 1},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                profile(**changes)

    def test_config_hash_excludes_identity_but_includes_behavior(self):
        a = ScoringConfiguration()
        self.assertEqual(a.config_hash, ScoringConfiguration().config_hash)
        self.assertNotEqual(
            a.config_hash, ScoringConfiguration(annual_hours=Decimal(1000)).config_hash
        )
        for changes in (
            {'evidence_weights': {}},
            {'annual_hours': 0},
            {'created_at': NOW.replace(tzinfo=None)},
            {'config_hash': 'forged'},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                ScoringConfiguration.model_validate(changes)

    def test_fx_validation_and_direction(self):
        self.assertEqual(fx().rates['INR'], 90)  # INR per EUR
        self.assertEqual(FXSnapshot.model_validate_json(canonical_json(fx())), fx())
        for changes in (
            {'rates': {'EUR': 2}},
            {'rates': {'EUR': 1, 'USD': 0}},
            {'rates': {'EUR': 1, 'ZZZ': 2}},
            {'provenance': 'static'},
            {'provenance': 'identity'},
            {'captured_at': NOW.replace(tzinfo=None)},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                fx(**changes)

    def test_compensation_unknown_and_inconsistent_ranges(self):
        self.assertIsNone(NormalizedCompensation().original_minimum)
        for changes in (
            {'original_minimum': 2, 'original_maximum': 1},
            {'annual_minimum': 1},
            {'evidence_quality': 'NaN'},
            {'assumed_annual_hours': 0},
            {'status': 'available'},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                NormalizedCompensation.model_validate(changes)

    def test_available_compensation_requires_complete_run_provenance(self):
        data = {
            'original_minimum': '10',
            'original_maximum': '20',
            'original_currency': 'EUR',
            'original_period': 'hourly',
            'original_basis': 'gross',
            'annual_minimum': '20800',
            'annual_maximum': '41600',
            'representative_value': '31200',
            'normalized_currency': 'EUR',
            'normalized_basis': 'gross',
            'normalization_method': 'midpoint',
            'normalizer_version': '1',
            'rounding_policy': 'decimal-half-even-2',
            'fx_snapshot_id': 'fx',
            'scoring_config_id': 'config',
            'assumed_annual_hours': '2080',
            'status': 'available',
        }
        value = NormalizedCompensation.model_validate(data)
        self.assertEqual(NormalizedCompensation.model_validate_json(canonical_json(value)), value)
        for changes in (
            {'normalized_basis': 'net'},
            {'assumed_annual_hours': None},
            {'representative_value': '50000'},
            {'representative_value': '1'},
            {'annual_minimum': '50000'},
            {'status': 'unavailable'},
            {'fx_snapshot_id': None},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                NormalizedCompensation.model_validate(data | changes)

    def test_job_hashes_follow_real_fields_and_not_filter_flags(self):
        job = Job(source='indeed', id='1', title=' Developer ', remote=True)
        first = JobVersion.capture(job, parser_version='1')
        job.flags = ['salary-unknown']
        self.assertEqual(
            first.content_hash, JobVersion.capture(job, parser_version='1').content_hash
        )
        job.remote = False
        changed = JobVersion.capture(job, parser_version='1')
        self.assertEqual(first.job_id, changed.job_id)
        self.assertNotEqual(first.ranking_hash, changed.ranking_hash)
        job.source = 'naukri'
        self.assertNotEqual(first.job_id, JobVersion.capture(job, parser_version='1').job_id)
