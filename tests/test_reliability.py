import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from applicant.cli import main
from applicant.domain.company_resolution import resolve_company
from applicant.domain.job import Job
from applicant.domain.research import CompanyIdentifier
from applicant.errors import ConfigError, SourceError
from applicant.reviews.ambitionbox import AmbitionBoxClient
from applicant.reviews.models import CompanyRating, Review
from applicant.services.application_attempts import AttemptJournal
from applicant.services.company_cache import CacheClock, CompanyCache
from applicant.services.reviews import fetch_reviews
from tests.fakes import FakeBoard

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


def saved_session(path, token='first-session'):
    path.write_text(
        json.dumps(
            {
                'cookies': [
                    {'name': 'li_at', 'value': token, 'domain': '.linkedin.com'},
                ]
            }
        ),
        encoding='utf-8',
    )


class ReliabilityTest(unittest.TestCase):
    def test_search_preserves_completed_board_when_next_board_is_interrupted(self):
        from applicant.infra.store.repositories import listing

        job = Job(
            source='linkedin',
            id='1234',
            title='Engineer',
            company='Acme',
            location='London',
            url='https://example.org/jobs/1234',
        )
        finished = FakeBoard([job])
        interrupted = FakeBoard(KeyboardInterrupt())
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'listings.json'
            with (
                patch('applicant.search.Jobs._client', side_effect=[finished, interrupted]),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                result = main(
                    [
                        '--no-log-file',
                        'search',
                        'Engineer',
                        '-s',
                        'linkedin',
                        'indeed',
                        '--output',
                        str(output),
                    ]
                )
            self.assertEqual(result, 130)
            self.assertEqual(listing(output, 'sqlite').load(), [job])
            self.assertTrue(finished.closed and interrupted.closed)

    def test_clock_adjustment_expiry_and_restart(self):
        wall = Mock(return_value=NOW)
        mono = Mock(return_value=0)
        clock = CacheClock(wall, mono)
        wall.return_value = NOW - timedelta(minutes=5)
        mono.return_value = 5
        with self.assertLogs('applicant.services.company_cache', level='DEBUG'):
            self.assertEqual(clock.current(), NOW + timedelta(seconds=5))
        wall.return_value = NOW + timedelta(days=7)
        self.assertFalse(clock.fresh(NOW.isoformat(), 7))
        self.assertTrue(clock.fresh((wall.return_value + timedelta(minutes=2)).isoformat(), 7))
        self.assertFalse(clock.fresh((wall.return_value + timedelta(minutes=3)).isoformat(), 7))
        self.assertFalse(clock.fresh('2026-11-01T00:00:00', 7))

    def test_cache_preserves_other_keys_and_keeps_concurrent_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'financials.json'
            cache = CompanyCache(path)
            cache.keep(
                'crunchbase', 'abc', 'public', 'abc', None, 5, {'amount': 3}, NOW.isoformat()
            )
            cache.keep(
                'crunchbase',
                'abc',
                'public',
                'abc',
                None,
                5,
                {'amount': 2},
                (NOW + timedelta(seconds=1)).isoformat(),
            )
            cache.keep('crunchbase', 'xyz', 'public', 'xyz', None, 5, {}, NOW.isoformat())
            kept = cache.entry('crunchbase', 'abc', 'public')
            assert kept is not None
            self.assertEqual(kept['payload'], {'amount': 3})
            self.assertIsNotNone(cache.entry('crunchbase', 'xyz', 'public'))

    def test_reviews_hit_does_not_fetch_and_refresh_appends_sqlite_history(self):
        from applicant.infra.store.sqlite import Store

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'reviews.json'
            rating = CompanyRating(
                'ambitionbox',
                'Acme',
                'https://www.ambitionbox.com/reviews/acme-reviews',
                '230',
                overall_rating=4.0,
                reviews=[Review(title='One')],
            )
            rating.identity_verified = True
            client = AmbitionBoxClient()
            self.addCleanup(client.close)
            with patch.object(client, 'fetch', return_value=rating) as fetch:
                clients = {'ambitionbox': client}
                fetch_reviews('Acme', clients, str(path), max_reviews=1, backend='sqlite')
                self.assertEqual(
                    fetch_reviews('Acme', clients, str(path), max_reviews=1, backend='sqlite')
                    .ratings[0]
                    .reviews[0]
                    .title,
                    'One',
                )
                fetch.assert_called_once()
                fetch_reviews(
                    'Acme', clients, str(path), max_reviews=1, refresh=True, backend='sqlite'
                )
                self.assertEqual(fetch.call_count, 2)
            with Store.beside(path) as store:
                self.assertEqual(len(store.rating_history('Acme')), 2)

    def test_financials_cache_rejects_unverified_refresh_without_touching_export(self):
        from applicant.errors import Unparseable
        from applicant.financials import CompanyFinancials, Money
        from applicant.financials.crunchbase import CrunchbaseClient
        from applicant.services.financials import track_financials

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'financials.json'
            value = CompanyFinancials(
                company='Acme',
                company_id='acme',
                source='crunchbase',
                total_funding=Money(amount=100, currency='USD'),
                funding_rounds_count=0,
            )
            value._identity_verified = True
            client = CrunchbaseClient(api_key='', profile_dir=None)
            with patch.object(client, 'fetch', return_value=value) as fetch:
                track_financials(['Acme'], {'crunchbase': client}, str(path))
                original = path.read_bytes()
                track_financials(['Acme'], {'crunchbase': client}, str(path))
                fetch.assert_called_once()
                value._identity_verified = False
                with self.assertRaises(Unparseable):
                    track_financials(['Acme'], {'crunchbase': client}, str(path), refresh=True)
            self.assertEqual(path.read_bytes(), original)

    def test_degraded_crunchbase_refresh_keeps_history_and_does_not_replace_cache(self):
        import pickle

        from applicant.financials import CompanyFinancials, FinancialsTracker, Money
        from applicant.financials.crunchbase import CrunchbaseClient
        from applicant.financials.models import FundingRound
        from applicant.services.company_cache import CompanyCache
        from applicant.services.financials import track_financials

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'financials.json'
            client = CrunchbaseClient(api_key='', profile_dir=None)
            self.addCleanup(client.close)
            complete = CompanyFinancials(
                company='Acme',
                company_id='acme',
                source='crunchbase',
                total_funding=Money(amount=100, currency='USD'),
                funding_rounds_count=0,
            )
            complete._identity_verified = True
            with patch.object(client, 'fetch', return_value=complete):
                track_financials(['Acme'], {'crunchbase': client}, str(path))
            cache = CompanyCache(path)
            original = cache.entry('crunchbase', 'acme', 'public')
            partial = CompanyFinancials(
                company='Acme',
                company_id='acme',
                source='crunchbase',
                rounds=[FundingRound(date='2025-01-01', round='Seed')],
                notes=['Some Crunchbase financial cards could not be verified or read.'],
            )
            partial._identity_verified = True
            partial._capture_degraded = True
            # The owned worker transports the model through a multiprocessing pipe.
            received = pickle.loads(pickle.dumps(partial))
            self.assertTrue(received._capture_degraded)
            with patch.object(client, 'fetch', return_value=received):
                track_financials(['Acme'], {'crunchbase': client}, str(path), refresh=True)
            self.assertEqual(cache.entry('crunchbase', 'acme', 'public'), original)
            latest = FinancialsTracker(path).latest('crunchbase:acme')
            assert latest is not None
            self.assertEqual(latest.total_funding, complete.total_funding)
            self.assertEqual(latest.rounds, partial.rounds)
            self.assertEqual(latest.notes, partial.notes)

    def test_concurrent_review_refresh_keeps_the_newer_export_and_both_observations(self):
        from applicant.files import read_document
        from applicant.infra.store.sqlite import Store

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'reviews.json'
            client = AmbitionBoxClient()
            self.addCleanup(client.close)
            clients = {'ambitionbox': client}

            def rating(value):
                result = CompanyRating(
                    'ambitionbox',
                    'Acme',
                    'https://www.ambitionbox.com/reviews/acme-reviews',
                    '230',
                    overall_rating=value,
                )
                result.identity_verified = True
                return result

            def refresh_while_fetching(*args, **kwargs):
                with patch.object(client, 'fetch', return_value=rating(4.5)):
                    fetch_reviews('Acme', clients, str(path), refresh=True, backend='sqlite')
                return rating(3.5)

            with patch.object(client, 'fetch', side_effect=refresh_while_fetching):
                fetch_reviews('Acme', clients, str(path), refresh=True, backend='sqlite')
            self.assertEqual(read_document(path)['overall_rating'], 4.5)
            with Store.beside(path) as store:
                self.assertEqual(len(store.rating_history('Acme')), 2)

    def test_uncertain_attempt_survives_reopen_and_consumes_retry_once(self):
        for backend in ('files', 'sqlite'):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                log = root / 'applications.csv'
                account = root / 'session.json'
                saved_session(account)
                job = {'id': '1', 'url': 'https://www.linkedin.com/jobs/view/1/'}
                journal = AttemptJournal(log, account, backend=backend)
                identifier = journal.begin(job)
                reopened = AttemptJournal(log, account, backend=backend)
                self.assertEqual(reopened.detail(identifier)['state'], 'uncertain')
                with self.assertRaises(SourceError):
                    reopened.begin(job)
                with self.assertRaises(ConfigError):
                    reopened.resolve(identifier, 'not-submitted')
                reopened.resolve(identifier, 'not-submitted', attested=True)
                with self.assertRaises(SourceError):
                    reopened.begin(job)
                retry = AttemptJournal(log, account, retry=identifier, backend=backend)
                self.assertNotEqual(retry.begin(job), identifier)
                with self.assertRaises(SourceError):
                    retry.begin(job)

    def test_cli_keeps_uncertain_when_portal_history_is_inconclusive(self):
        from applicant.services.application_attempts import HISTORY

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log, session = root / 'applications.csv', root / 'session.json'
            saved_session(session)
            job = {'id': '1', 'url': 'https://www.linkedin.com/jobs/view/1/'}
            journal = AttemptJournal(log, session)
            identifier = journal.begin(job)
            argv = [
                '--no-log-file',
                '--store',
                'files',
                'apply',
                '--log',
                str(log),
                '--cookies',
                str(session),
                '--reconcile',
                identifier,
                '--outcome',
                'not-submitted',
            ]
            output = io.StringIO()
            interaction = Mock()
            interaction.ask.side_effect = ['history unavailable', 'verified not submitted']
            with (
                patch('applicant.cli.apply.sys.stdin.isatty', return_value=True),
                patch('applicant.cli.apply.terminal', return_value=interaction),
                patch('applicant.search.Jobs') as client,
                redirect_stdout(output),
                redirect_stderr(io.StringIO()),
            ):
                self.assertNotEqual(main(argv), 0)
                self.assertEqual(journal.detail(identifier)['state'], 'uncertain')
                self.assertEqual(main(argv), 0)
                client.assert_not_called()
            self.assertIn(job['url'], output.getvalue())
            self.assertIn(HISTORY, output.getvalue())
            self.assertEqual(journal.detail(identifier)['state'], 'not-submitted')

    def test_cli_retry_requires_acknowledgement_and_dispatches_exact_attempt_once(self):
        from applicant.boards.linkedin_apply import LinkedIn
        from applicant.infra.store.repositories import applications

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log, session = root / 'applications.csv', root / 'session.json'
            saved_session(session)
            job = Job(
                source='linkedin',
                id='321',
                title='Engineer',
                url='https://www.linkedin.com/jobs/view/321/',
            )
            journal = AttemptJournal(log, session, backend='sqlite')
            attempt = journal.begin(job.to_dict())
            journal.resolve(attempt, 'not-submitted', attested=True)
            argv = [
                '--no-log-file',
                '--store',
                'sqlite',
                'apply',
                '--log',
                str(log),
                '--cookies',
                str(session),
                '--retry-attempt',
                attempt,
                '--yes',
            ]
            adapter = LinkedIn()
            self.addCleanup(adapter.close)
            page, submit = Mock(url=job.url), Mock()
            page.locator.side_effect = lambda selector: (
                Mock(first=submit) if 'Submit application' in selector else Mock()
            )
            with (
                patch('applicant.search.Jobs.linkedin', return_value=adapter),
                patch.object(adapter, '_start', return_value=page),
                patch.object(adapter, '_confirmed', return_value=True),
                patch('applicant.boards.linkedin_apply._prepare_submit', return_value=submit.click),
                patch('applicant.boards.linkedin_apply.looks_blocked', return_value=False),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()) as diagnostics,
            ):
                self.assertEqual(main(argv), 1)
                self.assertIn('requires --acknowledge-duplicate-risk', diagnostics.getvalue())
                self.assertEqual(journal.detail(attempt)['state'], 'not-submitted')
                self.assertEqual(main([*argv, '--acknowledge-duplicate-risk']), 0)
                self.assertEqual(main([*argv, '--acknowledge-duplicate-risk']), 1)
                self.assertIn('must be reconciled as not-submitted', diagnostics.getvalue())
                submit.click.assert_called_once()
            self.assertEqual(journal.detail(attempt)['state'], 'retried')
            self.assertEqual(
                [(r['id'], r['status']) for r in applications(log, 'sqlite').rows()],
                [('321', 'not_submitted'), ('321', 'applied')],
            )

    def test_timeout_at_click_cannot_create_a_second_submission(self):
        from applicant.boards.linkedin_apply import UNCONFIRMED_SUBMIT, LinkedIn

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = LinkedIn()
            adapter.attempt_journal = AttemptJournal(root / 'apply.csv', root / 'state.json')
            page = Mock(url='https://www.linkedin.com/jobs/view/1/')
            submit = Mock()
            submit.click.side_effect = TimeoutError
            page.locator.side_effect = lambda selector: (
                Mock(first=submit) if 'Submit application' in selector else Mock()
            )
            job = {'id': '1', 'url': page.url}
            with (
                patch('applicant.boards.linkedin_apply.looks_blocked', return_value=False),
                patch('applicant.boards.linkedin_apply._prepare_submit', return_value=submit.click),
            ):
                self.assertEqual(adapter._apply_one(page, job), UNCONFIRMED_SUBMIT)
                with self.assertRaises(SourceError):
                    adapter._apply_one(page, job)
            submit.click.assert_called_once()

    def test_profile_blocks_duplicate_across_destinations_and_backends(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / 'session.json'
            job = {'id': '15', 'url': 'https://www.linkedin.com/jobs/view/15/'}
            identifier = AttemptJournal(root / 'one.csv', profile).begin(job, armed=True)
            other = AttemptJournal(root / 'other' / 'two.csv', profile, backend='sqlite')
            with self.assertRaisesRegex(SourceError, identifier):
                other.begin(job)
            other.recover_armed()
            self.assertEqual(other.detail(identifier)['state'], 'uncertain')
            self.assertFalse((root / 'other' / 'applicant.db').exists())

    def test_interrupted_unverified_financials_leaves_history_untouched(self):
        from applicant.financials.crunchbase import CrunchbaseClient
        from applicant.financials.models import CompanyFinancials, Money
        from applicant.infra.company_worker import CompanyFetchInterrupted
        from applicant.services.financials import track_financials

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'finance.json'
            original = b'{"companies": {}}'
            path.write_bytes(original)
            client = CrunchbaseClient()
            self.addCleanup(client.close)
            partial = CompanyFinancials(
                source='crunchbase',
                company='Acme',
                company_id='acme',
                url='https://www.crunchbase.com/organization/acme',
                total_funding=Money(amount=100, currency='USD'),
            )
            with (
                patch.object(client, 'fetch', side_effect=CompanyFetchInterrupted(partial)),
                self.assertRaises(KeyboardInterrupt),
            ):
                track_financials(['Acme'], {'crunchbase': client}, str(path), pause=0)
            self.assertEqual(path.read_bytes(), original)

    def test_key_rotation_and_session_changes_block_retry_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / 'application.csv'
            profile = root / 'session.json'
            saved_session(profile)
            job = {'id': '15', 'url': 'https://www.linkedin.com/jobs/view/15/'}
            journal = AttemptJournal(log, profile)
            identifier = journal.begin(job)
            journal.resolve(identifier, 'not-submitted', attested=True)
            retry = AttemptJournal(log, profile, retry=identifier)
            saved_session(profile, 'second-session')
            with self.assertRaisesRegex(ConfigError, 'session changed'):
                retry.begin(job)
            journal.rotate()
            with self.assertRaisesRegex(ConfigError, 'key rotated'):
                retry.begin(job)
            self.assertEqual(journal.detail(identifier)['state'], 'not-submitted')

    def test_dated_provider_aliases_do_not_leak_across_cutoffs(self):
        earlier = NOW - timedelta(days=1)
        later = NOW + timedelta(days=1)
        common = {
            'provider': 'crunchbase',
            'provider_company_id': 'acme',
            'verification_url': 'https://www.crunchbase.com/organization/acme',
        }
        aliases = [
            CompanyIdentifier.model_validate(
                {
                    **common,
                    'identifier_id': 'old',
                    'entity_id': 'acme',
                    'verified_at': earlier,
                    'effective_to': later.date(),
                }
            ),
            CompanyIdentifier.model_validate(
                {
                    **common,
                    'identifier_id': 'future',
                    'entity_id': 'other',
                    'verified_at': later,
                    'effective_from': later.date(),
                }
            ),
        ]
        self.assertEqual(
            resolve_company(
                aliases,
                effective_date=NOW.date(),
                knowledge_cutoff=NOW,
                provider='crunchbase',
                provider_id='acme',
            ),
            'acme',
        )
        self.assertIsNone(
            resolve_company(
                aliases,
                effective_date=later.date(),
                knowledge_cutoff=NOW,
                provider='crunchbase',
                provider_id='acme',
            )
        )
