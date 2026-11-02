"""Run-specific salary tiers are frozen without rewriting posting snapshots."""

import unittest

from pydantic import ValidationError

from applicant.domain.canonical import canonical_json
from applicant.domain.ranking_v2 import RankingRunV2, RankingRunV3
from applicant.infra.store.ranking_codecs import RUN_CODEC
from tests.ranking_fixtures import NOW, unknown_result


class RunChoiceTest(unittest.TestCase):
    def test_version_dispatch_and_legacy_payload_remain_distinct(self):
        fields = {
            'profile_id': 'p',
            'profile_revision': 1,
            'scoring_config_id': 'config',
            'fx_snapshot_id': 'fx',
            'build_identity': 'fixture',
            'lockfile_digest': 'a' * 64,
            'calculated_at': NOW,
            'job_version_ids': ('version',),
            'results': (unknown_result(),),
        }
        old = RankingRunV2.model_validate(fields)
        saved = canonical_json(old)
        self.assertEqual(RUN_CODEC.validate_json(saved), old)
        new = RankingRunV3.model_validate(fields | {'selected_tiers': {'job': 'Senior'}})
        self.assertEqual(RUN_CODEC.validate_json(canonical_json(new)), new)
        self.assertEqual(new.selected_tiers, {'job': 'Senior'})
        self.assertEqual(canonical_json(old), saved)
        with self.assertRaises(ValidationError):
            RankingRunV2.model_validate_json(canonical_json(new))
        with self.assertRaises(ValidationError):
            RankingRunV3.model_validate(fields | {'selected_tiers': {'other-job': 'Senior'}})
