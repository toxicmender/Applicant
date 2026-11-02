"""New-run fallback is explicit, persisted, isolated from historical loading."""

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

import httpx

from applicant.domain.canonical import canonical_json
from applicant.domain.ranking_v2 import FXSnapshotV2, ScoringConfigurationV2
from applicant.errors import StoreError, Unreachable
from applicant.infra.fx import FrankfurterFX
from applicant.infra.store.snapshots import FXSnapshotRepository
from applicant.infra.store.sqlite import Store
from applicant.services.ranking_fx import resolve_fx
from applicant.services.ranking_history import save_fx
from tests.ranking_fixtures import FIXTURES, NOW
from tests.test_ranking_contracts import fx


class RankingFXTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'applicant.db'
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.config = ScoringConfigurationV2()

    def resolve(self, **changes):
        return resolve_fx(
            self.store,
            **(
                {
                    'target_currency': 'EUR',
                    'source_currencies': {'USD'},
                    'config': self.config,
                    'evaluated_at': NOW,
                    'offline': True,
                }
                | changes
            ),
        )

    def test_live_partial_snapshot_is_not_merged_with_cache(self):
        save_fx(self.store, fx(rates={'EUR': 1, 'JPY': 150}))
        calls = []

        def handler(request):
            self.assertFalse(self.store.db.in_transaction)
            calls.append(request)
            return httpx.Response(200, content=(FIXTURES / 'ecb-rates.json').read_bytes())

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = self.resolve(
                offline=False,
                source_currencies={'USD', 'JPY'},
                provider=FrankfurterFX(client=client),
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.snapshot.provenance, 'live')
        self.assertEqual(result.missing_currencies, ('JPY',))
        self.assertIn('fx_live', result.diagnostics)
        self.assertIn('fx_missing_rate:JPY', result.diagnostics)
        self.assertEqual(
            FXSnapshotRepository(self.store.db).get(result.snapshot.snapshot_id), result.snapshot
        )

    def test_cache_copy_preserves_original_retrieval_and_exact_historical_record(self):
        original = save_fx(
            self.store,
            fx(
                rates={'EUR': 1, 'USD': '1.25'},
                effective_date=(NOW - timedelta(days=3)).date(),
                retrieved_at=NOW - timedelta(days=2),
                captured_at=NOW - timedelta(days=1),
            ),
        )
        expected = canonical_json(original)
        provider = mock.Mock()
        provider.fetch.side_effect = Unreachable('offline')
        result = self.resolve(offline=False, provider=provider)
        self.assertEqual(result.snapshot.provenance, 'cache')
        self.assertEqual(result.snapshot.retrieved_at, original.retrieved_at)
        self.assertEqual(result.snapshot.effective_date, original.effective_date)
        self.assertEqual(result.snapshot.captured_at, NOW)
        assert isinstance(result.snapshot, FXSnapshotV2)
        self.assertEqual(result.snapshot.parent_snapshot_id, original.snapshot_id)
        self.assertIn('fx_provider_unavailable:unreachable', result.diagnostics)
        save_fx(self.store, fx(snapshot_id='new', rates={'EUR': 1, 'USD': '2'}))
        with (
            Store(self.path) as reopened,
            mock.patch('httpx.Client.request', side_effect=AssertionError('network')),
        ):
            repo = FXSnapshotRepository(reopened.db)
            self.assertEqual(canonical_json(repo.get(original.snapshot_id)), expected)
            self.assertEqual(repo.get(result.snapshot.snapshot_id), result.snapshot)

    def test_no_conversion_and_offline_never_fetch(self):
        with mock.patch(
            'applicant.infra.fx.FrankfurterFX.fetch', side_effect=AssertionError('network')
        ):
            same = self.resolve(source_currencies={'EUR'}, offline=False)
            missing = self.resolve()
        self.assertEqual(same.diagnostics, ('fx_identity',))
        self.assertEqual(same.snapshot.rates, {'EUR': 1})
        self.assertEqual(missing.snapshot.provenance, 'identity')
        self.assertEqual(missing.missing_currencies, ('USD',))
        self.assertIn('fx_unavailable', missing.diagnostics)

    def test_live_cache_copy_keeps_response_metadata(self):
        with httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, content=(FIXTURES / 'ecb-rates.json').read_bytes())
            )
        ) as client:
            live = self.resolve(offline=False, provider=FrankfurterFX(client=client))
        copied = self.resolve()
        assert isinstance(copied.snapshot, FXSnapshotV2)
        assert isinstance(live.snapshot, FXSnapshotV2)
        self.assertEqual(copied.snapshot.parent_snapshot_id, live.snapshot.snapshot_id)
        self.assertEqual(copied.snapshot.raw_response, live.snapshot.raw_response)
        self.assertEqual(copied.snapshot.response_digest, live.snapshot.response_digest)
        self.assertEqual(copied.snapshot.request_url, live.snapshot.request_url)
        self.assertEqual(copied.snapshot.retrieved_at, live.snapshot.retrieved_at)

    def test_supplied_snapshot_is_exact_even_when_partial(self):
        supplied = fx(rates={'EUR': 1, 'INR': 90})
        result = self.resolve(supplied=supplied)
        self.assertEqual(result.snapshot, supplied)
        self.assertEqual(result.missing_currencies, ('USD',))
        # A missing target means no cross-pair is usable even if its source quote exists.
        result = self.resolve(
            supplied=supplied, target_currency='GBP', source_currencies={'INR', 'GBP'}
        )
        self.assertEqual(result.missing_currencies, ('INR',))

    def test_static_fallback_needs_opt_in_age_and_pair_coverage(self):
        static = fx(provenance='static', dataset_version='fixture', rates={'EUR': 1, 'USD': '1.25'})
        self.assertIn('fx_static_unusable', self.resolve(static=static).diagnostics)
        config = ScoringConfigurationV2(allow_static_fx=True)
        result = self.resolve(static=static, config=config)
        self.assertEqual(result.snapshot, static)
        self.assertIn('fx_static', result.diagnostics)
        self.assertEqual(self.resolve(supplied=static, config=config).snapshot, static)
        stale = fx(
            provenance='static',
            dataset_version='fixture',
            effective_date=(NOW - timedelta(days=8)).date(),
        )
        self.assertEqual(self.resolve(static=stale, config=config).snapshot.provenance, 'identity')

    def test_unusable_live_response_falls_back_and_stale_cache_cannot_refresh_itself(self):
        old = b'[{"date":"2026-10-01","base":"EUR","quote":"USD","rate":1.25}]'
        save_fx(
            self.store,
            fx(effective_date=(NOW - timedelta(days=8)).date(), rates={'EUR': 1, 'USD': 1}),
        )
        with httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=old))
        ) as client:
            result = self.resolve(offline=False, provider=FrankfurterFX(client=client))
        self.assertEqual(result.snapshot.provenance, 'identity')
        self.assertIn('fx_live_unusable', result.diagnostics)
        with httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b'invalid'))
        ) as client:
            result = self.resolve(offline=False, provider=FrankfurterFX(client=client))
        self.assertIn('fx_provider_unavailable:unparseable', result.diagnostics)

    def test_bad_caller_inputs_and_corrupt_history_fail_instead_of_falling_back(self):
        with self.assertRaises(ValueError):
            self.resolve(supplied=fx(effective_date=(NOW - timedelta(days=8)).date()))
        with self.assertRaises(ValueError):
            self.resolve(supplied=fx(provenance='static', dataset_version='fixture'))
        with self.assertRaises(ValueError):
            self.resolve(static=fx())
        with self.assertRaises(ValueError):
            self.resolve(target_currency='ZZZ')
        with self.store.db:
            self.store.db.execute('INSERT INTO fx_snapshots VALUES (?, ?)', ('corrupt', '{}'))
        with self.assertRaises(StoreError):
            self.resolve()

    def test_existing_transaction_prevents_network_and_composed_writes(self):
        self.store.db.execute('BEGIN IMMEDIATE')
        with self.assertRaisesRegex(StoreError, 'active transaction'):
            self.resolve(offline=False)
        self.store.db.rollback()

    def test_future_supplied_snapshot_and_static_without_coverage_are_unusable(self):
        tomorrow = NOW + timedelta(days=1)
        with self.assertRaisesRegex(ValueError, 'future-dated'):
            self.resolve(supplied=fx(captured_at=tomorrow, retrieved_at=tomorrow))
        static = fx(provenance='static', dataset_version='fixture')
        result = self.resolve(static=static, config=ScoringConfigurationV2(allow_static_fx=True))
        self.assertEqual(result.snapshot.provenance, 'identity')
        self.assertIn('fx_static_unusable', result.diagnostics)
