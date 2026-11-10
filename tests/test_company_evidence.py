"""Verified company evidence journeys over synthetic fixtures and recorded pages."""

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest import mock

import httpx
from hypothesis import given
from hypothesis import strategies as st

from applicant.cli import main
from applicant.domain.canonical import canonical_json, content_hash
from applicant.domain.company_evidence import (
    CompanyAssociation,
    CompanyEnrichment,
    CompanyEvidenceImport,
    CompanyEvidenceSnapshot,
    CompanyResponse,
    CompanySelection,
)
from applicant.domain.ranking import JobVersion
from applicant.domain.ranking_engine import captured_facts
from applicant.errors import SourceError, StoreError, Unparseable
from applicant.financials import CrunchbaseClient, TracxnClient
from applicant.financials.models import CompanyFinancials
from applicant.infra.company_sources import CompanyRecorder, company_client
from applicant.infra.store.company_evidence import (
    CompanyEvidenceNotFound,
    CompanyEvidenceRepository,
)
from applicant.infra.store.job_versions import JobVersionRepository
from applicant.infra.store.ranking_common import write_transaction
from applicant.infra.store.sqlite import MIGRATIONS, Store
from applicant.reviews import AmbitionBoxClient, GlassdoorClient
from applicant.services.company_evidence import (
    capture_company_evidence,
    enrich_company_evidence,
    import_company_evidence,
    rating_payload,
    show_company_evidence,
    validate_payload,
)
from applicant.services.ranking_read import read_run
from applicant.services.ranking_replay import replay_run
from applicant.settings import Settings
from tests import test_financials as financial_fixtures
from tests import test_ranking_cli as creation
from tests.fakes import FakePage, Visit, patched_browser
from tests.ranking_fixtures import NOW
from tests.test_financials import CB_ORG, TX_ID
from tests.test_financials_web import CB_PAGE, ng_state
from tests.test_ranking_engine import version
from tests.test_reviews import PAGE_PROPS, next_data_html
from tests.test_session_flows import apollo_html, reviews

FIXTURES = Path(__file__).parent / 'fixtures' / 'company_evidence'
IDS = {'glassdoor': '42', 'ambitionbox': '101', 'crunchbase': 'acme', 'tracxn': 'a' * 24}
LOCATORS = {
    'glassdoor': 'https://www.glassdoor.com/Reviews/Acme-Reviews-E42.htm',
    'ambitionbox': 'acme',
    'crunchbase': 'https://www.crunchbase.com/organization/acme',
    'tracxn': 'a' * 24,
}


def fixture(name):
    return json.loads((FIXTURES / (name + '.json')).read_text(encoding='utf-8'))


def association(job=None, provider='glassdoor', **updates):
    job = job or version('company')
    return CompanyAssociation.model_validate(
        {
            'association_id': provider,
            'job_id': job.job_id,
            'job_version_id': job.job_version_id,
            'provider': provider,
            'provider_company_id': IDS[provider],
            'locator': LOCATORS[provider],
            'verified_at': NOW - timedelta(days=2),
            'verified_by': 'candidate',
            'verification_note': 'Explicitly verified against the posting company website',
            'verification_url': 'https://example.com/company',
        }
        | updates
    )


def snapshot(associated=None, **updates):
    a = associated or association()
    data = fixture('rating' if a.provider in ('glassdoor', 'ambitionbox') else 'financials')
    data.update(source=a.provider, company_id=a.provider_company_id)
    if a.provider == 'ambitionbox':
        data['rating_breakdown'] = {'company_culture': 4.1, 'job_security': 3.8}
    return CompanyEvidenceSnapshot.model_validate(
        {
            'snapshot_id': a.association_id + '-snapshot',
            'association_id': a.association_id,
            'provider': a.provider,
            'provider_company_id': a.provider_company_id,
            'acquired_at': NOW,
            'observed_at': NOW - timedelta(days=1),
            'observation_note': 'Source observation date verified in fixture',
            'payload': data,
            'responses': (
                CompanyResponse.capture(
                    'https://example.com/provider', canonical_json(data).encode()
                ),
            ),
        }
        | updates
    )


def selection(*snapshots):
    return CompanyEnrichment(
        selections=tuple(
            CompanySelection(association_id=s.association_id, snapshot_id=s.snapshot_id)
            for s in snapshots
        )
    )


class CompanyContractTest(unittest.TestCase):
    @given(st.decimals(min_value=1, max_value=5, places=3, allow_nan=False, allow_infinity=False))
    def test_canonical_numeric_imports_preserve_snapshot_identity(self, value):
        base = fixture('rating')
        a = snapshot(payload=base | {'rating_breakdown': {'culture_and_values': float(value)}})
        b = snapshot(payload=base | {'rating_breakdown': {'culture_and_values': value}})
        self.assertEqual(a.payload, b.payload)
        self.assertEqual(CompanyEvidenceSnapshot.model_validate_json(canonical_json(a)), a)

    def test_strict_associations_and_snapshot_contracts(self):
        for provider in IDS:
            self.assertEqual(association(provider=provider).provider_company_id, IDS[provider])
            with self.assertRaises(ValueError):
                association(provider=provider, locator='Similar Company Name')
        tx = association(
            provider='tracxn',
            provider_company_id='__profile',
            locator='https://tracxn.com/d/companies/acme/__profile',
        )
        self.assertEqual(tx.provider_company_id, '__profile')
        a = association()
        sample = snapshot(a)
        for patch in (
            {'observed_at': NOW + timedelta(seconds=1)},
            {'payload': sample.payload | {'source': 'other'}},
            {'payload': sample.payload | {'company_id': 'other'}},
            {'responses': sample.responses * 2},
            {'payload': sample.payload | {'value': float('nan')}},
            {'unknown': True},
            {'responses': ()},
        ):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                CompanyEvidenceSnapshot.model_validate(sample.model_dump() | patch)
        for document in (
            lambda: CompanyEvidenceImport(associations=(a, a)),
            lambda: CompanyEvidenceImport(snapshots=(sample, sample)),
            lambda: CompanyEnrichment(selections=selection(sample).selections * 2),
        ):
            with self.assertRaises(ValueError):
                document()
        with self.assertRaises(ValueError):
            CompanyResponse.model_validate(
                sample.responses[0].model_dump() | {'body_sha256': '0' * 64}
            )
        with self.assertRaisesRegex(ValueError, 'limits'):
            snapshot(
                responses=(
                    CompanyResponse.capture('https://example.com/oversize', b' ' * 8_000_001),
                )
            )

    def test_strict_provider_payloads(self):
        sample = snapshot()
        validate_payload(sample)
        validate_payload(snapshot(association(provider='crunchbase')))
        for patch in (
            {'extra': 1},
            {'reviews': {}},
            {'reviews': [{'extra': 1}]},
            {'rating_breakdown': []},
            {'rating_breakdown': {'culture_and_values': 'invalid'}},
            {'review_count': True},
            {'rating_breakdown': {'culture_and_values': 6}},
            {'rating_breakdown': {'culture_and_values': True}},
        ):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                rating_payload(sample.payload | patch)
        data = snapshot(association(provider='crunchbase'))
        for patch in (
            {'extra': 1},
            {'rounds': {}},
            {'rounds': [{'extra': 1}]},
            {'total_funding': {'extra': 1}},
            {'rounds': [{'amount': {'extra': 1}}]},
        ):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                validate_payload(data.model_copy(update={'payload': data.payload | patch}))

    def test_bounded_recorder_and_safe_urls(self):
        recorder = CompanyRecorder()
        with self.assertRaises(Unparseable):
            recorder.completed()
        recorder(
            'https://user:secret@example.com/company?token=secret#fragment',
            b'payload',
            'http_response',
        )
        recorder('https://example.com/company', b'payload', 'http_response')
        self.assertEqual(len(recorder.completed()), 1)
        self.assertEqual(recorder.completed()[0].source_url, 'https://example.com/company')
        for setup, body in (
            (lambda r: None, b' ' * 8_000_001),
            (lambda r: setattr(r, 'size', 16_000_000), b'x'),
            (lambda r: setattr(r, 'responses', dict.fromkeys(map(str, range(64)))), b'x'),
        ):
            r = CompanyRecorder()
            setup(r)
            with self.assertRaises(Unparseable):
                r('https://example.com', body, 'browser_dom')
            with self.assertRaises(Unparseable):
                r.completed()

    def test_client_factory_uses_explicit_provider_modes(self):
        settings = Settings.load(
            data_dir='.', store='sqlite', crunchbase_key='cb-test', tracxn_key='tx-test'
        )
        for provider, constructor in (
            ('glassdoor', 'GlassdoorClient'),
            ('ambitionbox', 'AmbitionBoxClient'),
            ('crunchbase', 'CrunchbaseClient'),
            ('tracxn', 'TracxnClient'),
        ):
            with mock.patch('applicant.infra.company_sources.' + constructor) as made:
                recorder = CompanyRecorder()
                company_client(association(provider=provider), recorder, settings=settings)
                self.assertIs(made.call_args.kwargs['capture'], recorder)
        with (
            mock.patch.object(settings.__class__, 'secret', return_value=''),
            self.assertRaises(ValueError),
        ):
            company_client(association(provider='tracxn'), CompanyRecorder(), settings=settings)
        with mock.patch('applicant.infra.company_sources.TracxnClient') as made:
            company_client(
                association(
                    provider='tracxn',
                    provider_company_id='__profile',
                    locator='https://tracxn.com/d/companies/acme/__profile',
                ),
                CompanyRecorder(),
                settings=settings,
            )
            self.assertEqual(made.call_args.kwargs['api_key'], '')


class CompanyAdapterCaptureTest(unittest.TestCase):
    def test_capture_mode_rejects_missing_wrong_or_ambiguous_structured_ids(self):
        client = GlassdoorClient(capture=CompanyRecorder())
        for payload in (
            {'employer': {'ratingOverall': 4.2}},
            {'employerId': 43},
            {'employerId': 42, 'employer_id': 43},
        ):
            with self.assertRaises(Unparseable):
                client._build([payload], [], 'Acme', '42', 'url')
        for payload in (
            {'employerId': 42},
            {'employer_id': '42'},
            {'__typename': 'Employer', 'id': 42},
        ):
            self.assertEqual(client._build([payload], [], 'Acme', '42', 'url').company_id, '42')
        cb = CrunchbaseClient(api_key='', capture=CompanyRecorder())
        for payload in (
            {'funding_total': {'value': 10}},
            {'identifier': {'permalink': 'other'}},
            {'identifier': {'permalink': 'acme', 'entity_def_id': 'person'}},
        ):
            with self.assertRaises(Unparseable):
                cb._build([payload], 'acme', 'Acme', 0)
        tx = TracxnClient(api_key='', capture=CompanyRecorder())
        with self.assertRaises(Unparseable):
            tx._record({'id': 'other', 'name': 'Acme', 'domain': 'acme.test'}, 'a' * 24)
        page = FakePage([(r'tracxn', Visit(body='Total Funding\n$10M'))])
        with patched_browser(page), self.assertRaises(Unparseable):
            tx.fetch('https://tracxn.com/d/companies/acme/__profile', 0)

    def test_http_clients_archive_exact_response_bodies_without_credentials(self):
        cases = (
            (
                lambda r: httpx.Response(200, text=next_data_html(PAGE_PROPS)),
                lambda h, s: AmbitionBoxClient(client=h, delay=0, capture=s),
                'tcs',
                '4',
                0,
            ),
            (
                lambda r: httpx.Response(200, json=CB_ORG),
                lambda h, s: CrunchbaseClient(api_key='test-secret', client=h, delay=0, capture=s),
                'https://www.crunchbase.com/organization/zomato',
                'zomato',
                20,
            ),
            (
                financial_fixtures.Tracxn().handler([]),
                lambda h, s: TracxnClient(api_key='test-secret', client=h, delay=0, capture=s),
                TX_ID,
                TX_ID,
                20,
            ),
        )
        for handler, make, locator, identity, count in cases:
            recorder = CompanyRecorder()
            with (
                httpx.Client(transport=httpx.MockTransport(handler)) as http,
                make(http, recorder) as client,
            ):
                result = client.fetch(locator, count)
            self.assertEqual(result.company_id, identity)
            archived = recorder.completed()
            self.assertTrue(all(r.representation == 'http_response' for r in archived))
            self.assertNotIn('test-secret', canonical_json(archived))
            if identity == 'zomato':
                self.assertEqual(json.loads(archived[0].body), CB_ORG)

    def response(self, url, payload):
        response = mock.Mock(url=url)
        response.json.return_value = payload
        response.body.return_value = json.dumps(payload).encode()
        return response

    def test_browser_clients_archive_dom_and_consumed_json(self):
        # These use the existing browser-flow fake: no browser installation or network.
        for provider in ('crunchbase', 'tracxn', 'glassdoor'):
            recorder = CompanyRecorder()
            if provider == 'crunchbase':
                response = self.response(
                    'https://www.crunchbase.com/v4/data/entity/zomato', CB_PAGE
                )
                page = FakePage(
                    [(r'organization/zomato', Visit(html=ng_state(CB_PAGE), responses=[response]))]
                )
                client = CrunchbaseClient(api_key='', capture=recorder)
                locator = 'https://www.crunchbase.com/organization/zomato'
                count = 0
            elif provider == 'tracxn':
                payload = {
                    'id': '__profile',
                    'name': 'Acme',
                    'totalMoneyRaised': {'totalAmount': {'amount': 100, 'currency': 'USD'}},
                }
                response = self.response('https://tracxn.com/api/company', payload)
                page = FakePage(
                    [(r'/d/companies/acme', Visit(html='<html>Acme</html>', responses=[response]))]
                )
                client = TracxnClient(api_key='', capture=recorder)
                locator = 'https://tracxn.com/d/companies/acme/__profile'
                count = 0
            else:
                payload = {
                    'employer': {'id': 42, 'ratingOverall': 4.2, 'reviewCount': 12},
                    'reviews': reviews(0, 1),
                }
                response = self.response('https://www.glassdoor.com/graph', payload)
                page = FakePage(
                    [
                        (r'E42\.htm$', Visit(html=apollo_html(payload), responses=[response])),
                        (r'_P2', Visit(html=apollo_html({'reviews': reviews(1, 1)}))),
                    ]
                )
                client = GlassdoorClient(capture=recorder)
                locator = 'https://www.glassdoor.com/Reviews/Acme-Reviews-E42.htm'
                count = 2
            with patched_browser(page), client:
                result = client.fetch(locator, count)
            self.assertEqual(result.source, provider)
            self.assertEqual(
                {r.representation for r in recorder.completed()},
                {'browser_response', 'browser_dom'},
            )
            self.assertIn(response.body.return_value, [r.body for r in recorder.completed()])


class CompanyStoreTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / 'company # 测试.db'
        self.job = version('company')
        with Store(self.path) as store, write_transaction(store.db):
            JobVersionRepository(store.db).put(self.job)
        self.a = association(self.job)
        self.s = snapshot(self.a)
        self.document = CompanyEvidenceImport(associations=(self.a,), snapshots=(self.s,))

    def test_idempotency_dry_runs_and_immutable_sql(self):
        for dry in (True, False, True, False):
            import_company_evidence(self.path, self.document, dry_run=dry)
        for dry in (True, False):
            for document in (
                CompanyEvidenceImport(
                    associations=(self.a.model_copy(update={'verification_note': 'changed'}),)
                ),
                CompanyEvidenceImport(snapshots=(snapshot(self.a, observation_note='changed'),)),
            ):
                with self.assertRaises(StoreError):
                    import_company_evidence(self.path, document, dry_run=dry)
        before = self.path.read_bytes()
        with mock.patch('httpx.Client.request', side_effect=AssertionError('network')):
            report = show_company_evidence(
                self.path, association_ids=('glassdoor',), snapshot_ids=('glassdoor-snapshot',)
            )
        self.assertEqual(report['snapshots'][0]['snapshot_hash'], self.s.snapshot_hash)
        self.assertEqual(self.path.read_bytes(), before)
        with Store(self.path) as store:
            repo = CompanyEvidenceRepository(store.db)
            for put in (lambda: repo.put_association(self.a), lambda: repo.put_snapshot(self.s)):
                with self.assertRaises(StoreError):
                    put()
            for table in ('company_associations', 'company_evidence_snapshots'):
                for sql in (
                    f'DELETE FROM {table}',
                    f'UPDATE {table} SET rowid=rowid',
                    f'INSERT OR REPLACE INTO {table} SELECT * FROM {table}',
                ):
                    with self.assertRaises(sqlite3.IntegrityError):
                        store.db.execute(sql)
                    store.db.rollback()
            with self.assertRaises(CompanyEvidenceNotFound):
                repo.association('missing')
            with self.assertRaises(CompanyEvidenceNotFound):
                repo.snapshot('missing')
            with self.assertRaises(sqlite3.IntegrityError):
                store.db.execute(
                    "INSERT INTO company_evidence_snapshots VALUES ('bad','missing','glassdoor','42','bad','{}')"
                )
            store.db.rollback()

    def test_exact_job_ids_matching_associations_and_atomic_rollback(self):
        for dry in (True, False):
            with self.assertRaises(ValueError):
                import_company_evidence(
                    self.path,
                    CompanyEvidenceImport(
                        associations=(self.a.model_copy(update={'job_id': 'wrong'}),)
                    ),
                    dry_run=dry,
                )
            wrong = snapshot(
                self.a,
                provider='ambitionbox',
                provider_company_id='101',
                payload=fixture('rating')
                | {'source': 'ambitionbox', 'company_id': '101', 'rating_breakdown': {}},
            )
            with self.assertRaises(ValueError):
                import_company_evidence(
                    self.path,
                    CompanyEvidenceImport(associations=(self.a,), snapshots=(wrong,)),
                    dry_run=dry,
                )
            with self.assertRaises(ValueError):
                import_company_evidence(
                    self.path,
                    CompanyEvidenceImport(
                        associations=(self.a,),
                        snapshots=(
                            snapshot(self.a, acquired_at=NOW - timedelta(days=3), observed_at=None),
                        ),
                    ),
                    dry_run=dry,
                )
        with Store.open_readonly(self.path) as store:
            self.assertEqual(
                store.db.execute('SELECT count(*) FROM company_associations').fetchone()[0], 0
            )
        missing = self.root / 'missing.db'
        with self.assertRaises(StoreError):
            import_company_evidence(missing, self.document, dry_run=True)
        self.assertFalse(missing.exists())

    def test_upgrade_every_prior_version_and_migration_rollback(self):
        for n in range(1, len(MIGRATIONS)):
            path = self.root / f'old-{n}.db'
            with mock.patch('applicant.infra.store.sqlite.MIGRATIONS', MIGRATIONS[:n]), Store(path):
                pass
            if n == 6:
                with self.assertRaisesRegex(StoreError, 'upgrade'):
                    Store.open_readonly(path)
            with Store(path) as store:
                self.assertEqual(
                    store.db.execute('PRAGMA user_version').fetchone()[0], len(MIGRATIONS)
                )
        path = self.root / 'broken.db'
        with mock.patch('applicant.infra.store.sqlite.MIGRATIONS', MIGRATIONS[:6]), Store(path):
            pass
        broken = (*MIGRATIONS[:6], (*MIGRATIONS[6], 'INVALID SQL'))
        with (
            mock.patch('applicant.infra.store.sqlite.MIGRATIONS', broken),
            self.assertRaises(StoreError),
        ):
            Store(path)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 6)
            self.assertIsNone(
                db.execute(
                    "SELECT name FROM sqlite_master WHERE name='company_associations'"
                ).fetchone()
            )

    def test_corrupt_keys_payloads_and_hashes(self):
        for table, sql in (
            ('company_associations', "UPDATE company_associations SET payload_hash='bad'"),
            ('company_associations', "UPDATE company_associations SET payload='{}'"),
            (
                'company_evidence_snapshots',
                "UPDATE company_evidence_snapshots SET snapshot_hash='bad'",
            ),
            ('company_evidence_snapshots', "UPDATE company_evidence_snapshots SET payload='{}'"),
        ):
            path = self.root / (table + str(len(sql)) + '.db')
            with Store(path) as store, write_transaction(store.db):
                JobVersionRepository(store.db).put(self.job)
            import_company_evidence(path, self.document)
            with Store(path) as store:
                store.db.execute(f'DROP TRIGGER immutable_{table}_update')
                store.db.execute(sql)
                store.db.commit()
                with self.assertRaises(StoreError):
                    repo = CompanyEvidenceRepository(store.db)
                    repo.association(
                        self.a.association_id
                    ) if table == 'company_associations' else repo.snapshot(self.s.snapshot_id)

        import_company_evidence(self.path, self.document)
        changed = self.a.model_copy(update={'verified_at': NOW + timedelta(days=1)})
        with Store(self.path) as store:
            store.db.execute('DROP TRIGGER immutable_company_associations_update')
            store.db.execute(
                'UPDATE company_associations SET payload_hash=?,payload=?',
                (content_hash(changed), canonical_json(changed)),
            )
            store.db.commit()
            with self.assertRaisesRegex(StoreError, 'historical company evidence association'):
                CompanyEvidenceRepository(store.db).snapshot(self.s.snapshot_id)

    def fake_factory(self, association, recorder, *, settings):
        client = mock.MagicMock()
        result = snapshot(association)
        recorder(
            'https://example.com/company', canonical_json(result.payload).encode(), 'http_response'
        )
        client.fetch.return_value = (
            rating_payload(result.payload)
            if association.provider in ('glassdoor', 'ambitionbox')
            else CompanyFinancials.model_validate(result.payload)
        )
        client.__enter__.return_value = client
        return client

    def test_four_provider_capture_refresh_and_partial_failure(self):
        associations = tuple(association(self.job, provider=p) for p in IDS)
        import_company_evidence(self.path, CompanyEvidenceImport(associations=associations))
        with mock.patch(
            'applicant.services.company_evidence.company_client', side_effect=self.fake_factory
        ):
            report = capture_company_evidence(
                self.path, tuple(IDS), settings=Settings(), clock=lambda: NOW
            )
        self.assertTrue(report['success'])
        self.assertEqual(len(report['outcomes']), 4)
        self.assertTrue(all(r['snapshot']['observed_at'] is None for r in report['outcomes']))
        with mock.patch(
            'applicant.services.company_evidence.company_client', side_effect=self.fake_factory
        ):
            refreshed = capture_company_evidence(
                self.path, ('glassdoor',), settings=Settings(), clock=lambda: NOW
            )
        self.assertNotEqual(
            refreshed['outcomes'][0]['snapshot']['snapshot_id'],
            report['outcomes'][0]['snapshot']['snapshot_id'],
        )

        def partial(a, recorder, *, settings):
            if a.provider == 'glassdoor':
                raise SourceError('source unavailable')
            return self.fake_factory(a, recorder, settings=settings)

        with mock.patch('applicant.services.company_evidence.company_client', side_effect=partial):
            result = capture_company_evidence(
                self.path, ('glassdoor', 'crunchbase'), settings=Settings(), clock=lambda: NOW
            )
        self.assertFalse(result['success'])
        self.assertEqual([o['status'] for o in result['outcomes']], ['failed', 'captured'])
        for ids in ((), ('glassdoor', 'glassdoor')):
            with self.assertRaises(ValueError):
                capture_company_evidence(self.path, ids, settings=Settings())

        def wrong(a, recorder, *, settings):
            client = self.fake_factory(a, recorder, settings=settings)
            client.fetch.return_value.company_id = 'wrong'
            return client

        with mock.patch('applicant.services.company_evidence.company_client', side_effect=wrong):
            self.assertFalse(
                capture_company_evidence(
                    self.path, ('glassdoor',), settings=Settings(), clock=lambda: NOW
                )['success']
            )


class CompanyJourneyTest(unittest.TestCase):
    def setUp(self):
        self.data = creation.RankingCliTest()
        self.data.setUp()
        self.addCleanup(self.data.doCleanups)
        self.path = self.data.database
        self.jobs = self.data.mixed_jobs()
        self.created = self.data.service(versions_path=self.data.bundle(self.jobs))
        self.a = association(self.jobs[0])
        self.s = snapshot(self.a)
        import_company_evidence(
            self.path, CompanyEvidenceImport(associations=(self.a,), snapshots=(self.s,))
        )

    def test_enrichment_ranking_and_unchanged_historical_replay(self):
        before = read_run(self.path, self.created.ranking_run_id)
        a2 = association(self.jobs[0], provider='crunchbase')
        s2 = snapshot(a2)
        import_company_evidence(
            self.path, CompanyEvidenceImport(associations=(a2,), snapshots=(s2,))
        )
        with mock.patch('httpx.Client.request', side_effect=AssertionError('network')):
            bundle = enrich_company_evidence(self.path, selection(self.s, s2), clock=lambda: NOW)
            repeated = enrich_company_evidence(
                self.path, selection(self.s, s2), clock=lambda: NOW + timedelta(days=1)
            )
        enriched = bundle.jobs[0]
        self.assertEqual(enriched.job_version_id, repeated.jobs[0].job_version_id)
        self.assertNotEqual(enriched.job_version_id, self.jobs[0].job_version_id)
        self.assertEqual(enriched.job_id, self.jobs[0].job_id)
        self.assertEqual(enriched.evidence[: len(self.jobs[0].evidence)], self.jobs[0].evidence)
        facts, _ = captured_facts(enriched)
        self.assertEqual(sum(len(f.ratings) for f in facts), 2)
        new = self.data.service(versions_path=self.data.bundle(bundle.jobs))
        report = read_run(self.path, new.ranking_run_id)
        result = report['groups']['eligible']['results'][0]
        self.assertEqual(len(result['company_evidence']), 2)
        self.assertNotIn('responses', result['company_evidence'][0])
        self.assertEqual(
            next(d for d in result['dimensions'] if d['dimension'] == 'company_culture')['status'],
            'known',
        )
        self.assertEqual(
            next(d for d in result['dimensions'] if d['dimension'] == 'stability')['status'],
            'unknown',
        )
        self.assertEqual(read_run(self.path, self.created.ranking_run_id), before)
        with Store.open_readonly(self.path) as store, store.read_snapshot():
            replay = replay_run(
                store,
                self.created.ranking_run_id,
                build_identity=self.created.build_identity,
                lockfile_digest=self.created.lockfile_digest,
            )
            self.assertTrue(replay.matches)

    def test_unknown_dates_stale_and_missing_counts_keep_existing_policies(self):
        for update, expected in (
            ({'observed_at': None}, 'unknown'),
            ({'observed_at': NOW - timedelta(days=181)}, 'unknown'),
            ({'payload': self.s.payload | {'review_count': None}}, 'known'),
        ):
            s = snapshot(self.a, snapshot_id='case-' + expected + str(len(str(update))), **update)
            import_company_evidence(self.path, CompanyEvidenceImport(snapshots=(s,)))
            bundle = enrich_company_evidence(self.path, selection(s), clock=lambda: NOW)
            result = self.data.service(versions_path=self.data.bundle(bundle.jobs)).results[0]
            self.assertEqual(
                next(d for d in result.dimensions if d.dimension == 'company_culture').status,
                expected,
            )

    def test_ambiguous_associations_wrong_selection_and_multiple_parent_versions(self):
        other = association(
            self.jobs[0],
            association_id='other',
            provider_company_id='43',
            locator='https://www.glassdoor.com/Reviews/Other-Reviews-E43.htm',
        )
        sample = snapshot(other)
        import_company_evidence(
            self.path, CompanyEvidenceImport(associations=(other,), snapshots=(sample,))
        )
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            enrich_company_evidence(self.path, selection(self.s, sample))
        wrong = CompanyEnrichment(
            selections=(
                CompanySelection(
                    association_id=self.a.association_id, snapshot_id=sample.snapshot_id
                ),
            )
        )
        with self.assertRaises(ValueError):
            enrich_company_evidence(self.path, wrong)
        newer = JobVersion.model_validate(
            self.jobs[0].model_dump() | {'job_version_id': 'changed', 'description': 'changed'}
        )
        with Store(self.path) as store, write_transaction(store.db):
            JobVersionRepository(store.db).put(newer)
        a3 = association(newer, association_id='newer')
        s3 = snapshot(a3)
        import_company_evidence(
            self.path, CompanyEvidenceImport(associations=(a3,), snapshots=(s3,))
        )
        with self.assertRaisesRegex(ValueError, 'multiple parent'):
            enrich_company_evidence(self.path, selection(self.s, s3))

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(
                ['--no-log-file', '--data-dir', str(self.data.root), 'company-evidence', *args]
            )
        return code, out.getvalue(), err.getvalue()

    def test_cli_strict_imports_offline_exports_and_enrichment(self):
        document = self.data.write(
            'evidence.json', CompanyEvidenceImport(associations=(self.a,), snapshots=(self.s,))
        )
        assoc = self.data.write('association.json', self.a)
        selected = self.data.write('selected.json', selection(self.s))
        exported = self.data.root / 'enriched # 测试.json'
        for action, path in (('associate', assoc), ('import', document)):
            self.assertEqual(self.cli(action, '--from-file', str(path), '--dry-run')[0], 0)
            self.assertEqual(self.cli(action, '--from-file', str(path))[0], 0)
        before = self.path.read_bytes()
        with mock.patch('httpx.Client.request', side_effect=AssertionError('network')):
            self.assertEqual(
                self.cli(
                    'show',
                    '--association',
                    self.a.association_id,
                    '--snapshot',
                    self.s.snapshot_id,
                    '--export',
                    str(self.data.root / 'report.json'),
                )[0],
                0,
            )
            self.assertEqual(
                self.cli('enrich', '--from-file', str(selected), '--export', str(exported))[0], 0
            )
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(
            json.loads(exported.read_text(encoding='utf-8'))['kind'], 'ranking_job_versions'
        )
        for options in (
            ('show', '--from-file', str(document)),
            ('capture', '--snapshot', 'x'),
            ('associate', '--association', 'x'),
            ('enrich', '--from-file', str(selected)),
        ):
            with self.subTest(options=options), self.assertRaises(SystemExit) as stopped:
                self.cli(*options)
            self.assertEqual(stopped.exception.code, 2)
        for options in (
            ('show',),
            ('show', '--snapshot', ' '),
            ('show', '--snapshot', 'x', '--export', str(self.path)),
        ):
            self.assertEqual(self.cli(*options)[0], 2)
        self.assertEqual(self.cli('show', '--snapshot', 'missing')[0], 4)
        document.write_text(
            '{"kind":"company_evidence_import","kind":"company_evidence_import"}', encoding='utf-8'
        )
        self.assertEqual(self.cli('import', '--from-file', str(document))[0], 2)
        for action in ('capture', 'refresh'):
            with mock.patch(
                'applicant.cli.company_evidence.capture_company_evidence',
                return_value={'success': False, 'outcomes': []},
            ):
                self.assertEqual(self.cli(action, '--association', self.a.association_id)[0], 1)
        with mock.patch(
            'applicant.cli.company_evidence.write_document', side_effect=OSError('disk unavailable')
        ):
            self.assertEqual(
                self.cli('show', '--snapshot', self.s.snapshot_id, '--export', str(exported))[0], 1
            )
