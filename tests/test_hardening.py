"""Tests for what the mutation pass (docs/mutation-report.md) showed the suite
would not notice: each one pins down a behaviour that a changed operator,
boundary or argument left the other tests passing.
"""

from __future__ import annotations

import io
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from datetime import date
from pathlib import Path
from typing import Any
from unittest import mock

from applicant.domain import dedupe, flags
from applicant.domain.capability import Capability, Field
from applicant.domain.dates import epoch_to_iso
from applicant.domain.filtering import JobFilter as PureFilter
from applicant.domain.job import Job, experience_from
from applicant.domain.places import _pattern
from applicant.domain.ports import ApplicationResult, Interrupted
from applicant.domain.rates import RateSnapshot
from applicant.domain.salary import number, parse_salary
from applicant.errors import Blocked
from applicant.filters import JobFilter
from applicant.services import financials as financials_service
from applicant.services import rates as rates_service
from applicant.services.apply import ApplyToJobs, EasyApply, easy_apply_with, select
from applicant.services.events import BoardSearched, RatingFetched, SourceFailed
from applicant.services.fanout import fan_out
from applicant.services.reviews import fetch_reviews
from applicant.services.search import Enrichment, SearchJobs, store
from applicant.services.status import summarise
from applicant.storage import ApplicationLog

TODAY = date(2026, 10, 5)


def job(source='indeed', n=1, **fields) -> Job:
    values: dict[str, Any] = {
        'title': 'Python Developer',
        'company': 'Acme',
        'url': f'https://{source}.example/{n}',
    }
    values.update(fields)
    return Job(source=source, id=f'{source}-{n}', **values)


class Folder(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)


# -- domain/salary ----------------------------------------------------------


class SalaryEdgesTest(unittest.TestCase):
    def test_a_bare_year_is_not_pay_at_both_ends_of_the_range(self):
        for text in ('1900', '2100'):
            with self.subTest(text=text):
                self.assertIsNone(parse_salary(text))
        for text, value in (('1899', 1899.0), ('2101', 2101.0)):
            with self.subTest(text=text):
                salary = parse_salary(text)
                assert salary is not None
                self.assertEqual(salary.high, value)

    def test_a_second_figure_not_joined_as_a_range_is_not_the_top(self):
        salary = parse_salary('$50,000 a year plus 80000 bonus')
        assert salary is not None
        self.assertEqual((salary.low, salary.high), (50000.0, 50000.0))

    def test_a_unit_makes_a_dotted_figure_a_decimal(self):
        salary = parse_salary('2.500 Lacs')
        assert salary is not None
        self.assertEqual(salary.high, 250000.0)
        self.assertEqual(number('2.500'), 2500.0, 'without a unit it is grouping')

    def test_whichever_separator_comes_last_is_the_decimal(self):
        self.assertEqual(number('1,234.56'), 1234.56)
        self.assertEqual(number('1.234,56'), 1234.56)
        self.assertEqual(number('1.234.567,8'), 1234567.8)

    def test_each_end_of_a_range_keeps_its_own_unit(self):
        salary = parse_salary('80k - 1.2L')
        assert salary is not None
        self.assertEqual((salary.low, salary.high), (80000.0, 120000.0))


# -- domain/job -------------------------------------------------------------


class ExperienceEdgesTest(unittest.TestCase):
    def test_a_minimum_beyond_a_working_life_is_not_believed(self):
        self.assertIsNone(experience_from('99+ years of experience'))
        self.assertIsNone(experience_from('2-99 years'))
        self.assertEqual(experience_from('5+ years'), ('5+ years', 5.0, None))


# -- domain/filtering -------------------------------------------------------


class FilterEdgesTest(unittest.TestCase):
    def test_pay_exactly_at_the_minimum_is_enough(self):
        rule = JobFilter(
            min_salary=1_000_000, currency='INR', rates=RateSnapshot(market={'USD': 1.0})
        )
        keep, _ = rule.matches(job(salary='₹10,00,000 a year'), today=TODAY)
        self.assertTrue(keep)

    def test_a_converted_salary_says_how_it_was_converted(self):
        rates = RateSnapshot(market={'USD': 1.0, 'INR': 80.0})
        rule = JobFilter(min_salary=1_000_000, currency='INR', rates=rates)
        keep, found = rule.matches(job(salary='$20,000 a year'), today=TODAY)
        self.assertTrue(keep)
        self.assertIn(flags.PPP_UNAVAILABLE, found)

    def test_a_pure_filter_uses_the_rates_it_was_given(self):
        rates = RateSnapshot(market={'USD': 1.0, 'INR': 80.0})
        keep, found = PureFilter(
            min_salary=1_000_000, currency='INR', rates=rates, salary_basis='market'
        ).matches(job(salary='$20,000 a year'), today=TODAY)
        self.assertTrue(keep)
        self.assertEqual(found, [])

    def test_one_bound_is_enough_to_rule_a_job_out(self):
        rule = JobFilter(experience=2)
        self.assertFalse(rule.matches(job(experience_min=5), today=TODAY)[0], 'needs 5, you have 2')
        self.assertFalse(rule.matches(job(experience_max=1), today=TODAY)[0], 'wants at most 1')

    def test_a_posted_timestamp_is_read_by_its_date(self):
        rule = JobFilter(posted_within_days=3)
        keep, found = rule.matches(job(posted='2026-10-04T09:30:00Z'), today=TODAY)
        self.assertTrue(keep)
        self.assertEqual(found, [])

    def test_a_currency_only_matters_with_a_minimum(self):
        self.assertEqual(JobFilter(currency='usd').currencies(), set())
        self.assertEqual(JobFilter(min_salary=10).currencies(), set())
        self.assertEqual(JobFilter(min_salary=10, currency='usd').currencies(), {'USD'})


# -- domain/dedupe ----------------------------------------------------------


class DedupeEdgesTest(unittest.TestCase):
    def test_every_part_of_the_key_counts(self):
        base = {'source': 'indeed', 'title': 'Dev', 'company': 'Acme', 'location': 'Pune'}
        for field, other in (('source', 'naukri'), ('company', 'Beta'), ('location', 'Delhi')):
            with self.subTest(field=field):
                self.assertNotEqual(dedupe.key(base), dedupe.key({**base, field: other}))

    def test_the_city_is_part_of_the_fingerprint(self):
        pune = dedupe.fingerprint({'title': 'Dev', 'company': 'Acme', 'location': 'Pune, India'})
        delhi = dedupe.fingerprint({'title': 'Dev', 'company': 'Acme', 'location': 'Delhi, India'})
        self.assertNotEqual(pune, delhi)

    def test_copies_without_a_fingerprint_are_never_merged(self):
        bare = [
            Job(source='indeed', id='1', title='Dev'),
            Job(source='naukri', id='2', title='Dev'),
        ]
        self.assertEqual(len(dedupe.one_per_job(bare)), 2)

    def test_equal_reach_keeps_the_first_copy(self):
        kept = dedupe.one_per_job([job('indeed', 1), job('naukri', 2)])
        self.assertEqual([one.source for one in kept], ['indeed'])
        self.assertEqual(kept[0].flags, ['also-on-naukri'])

    def test_easy_apply_wins_over_an_earlier_plain_url(self):
        kept = dedupe.one_per_job([job('indeed', 1), job('linkedin', 2, easy_apply=True)])
        self.assertEqual([one.source for one in kept], ['linkedin'])

    def test_three_copies_become_one_and_keep_their_own_flags(self):
        first = job('indeed', 1)
        first.flags = ['salary-unknown']
        winner = job('linkedin', 2, easy_apply=True)
        winner.flags = ['location-unverified']
        kept = dedupe.one_per_job([first, winner, job('naukri', 3)])
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].flags, ['location-unverified', 'also-on-indeed', 'also-on-naukri'])


# -- domain/places, domain/dates --------------------------------------------


class PlacesAndDatesTest(unittest.TestCase):
    def test_the_longer_place_name_wins(self):
        self.assertEqual(
            _pattern(['delhi', 'new delhi']).findall('jobs in new delhi'), ['new delhi']
        )
        self.assertEqual(_pattern(['york', 'new york']).findall('new york'), ['new york'])
        # a prefix sorts first alphabetically: the order must not be the reason
        self.assertEqual(_pattern(['new', 'new york']).findall('new york'), ['new york'])

    @unittest.skipUnless(hasattr(time, 'tzset'), 'needs a POSIX tzset')
    def test_an_epoch_is_read_in_utc_whatever_the_machine_s_zone(self):
        """23:00 UTC on 4 October 2026 is already the 5th at UTC+14.

        (Not a 1970 instant: Kiritimati was then UTC-10:40, the same day.)
        """
        with mock.patch.dict(os.environ, {'TZ': 'Pacific/Kiritimati'}):
            time.tzset()
            try:
                self.assertEqual(epoch_to_iso(1_791_154_800_000), '2026-10-04')
            finally:
                os.environ.pop('TZ', None)
        time.tzset()


# -- services ---------------------------------------------------------------


class ApplyServiceEdgesTest(Folder):
    def test_a_client_that_keeps_no_record_of_its_outcomes(self):
        client = mock.Mock(spec=['easy_apply'])
        client.easy_apply.return_value = ['https://linkedin.example/1']
        entries = easy_apply_with(client, [job('linkedin', 1), job('linkedin', 2)])
        self.assertEqual([status for _, status, _ in entries], ['applied', 'needs_manual_apply'])

    def test_a_client_s_failed_jobs_are_failed_rows(self):
        client = mock.Mock(spec=['easy_apply', 'failed'])
        client.easy_apply.return_value = []
        client.failed = {'https://linkedin.example/1': 'TimeoutError'}
        (entry,) = easy_apply_with(client, [job('linkedin', 1)])
        self.assertEqual(entry[1:], ('failed', 'TimeoutError'))

    def test_an_interrupt_carries_what_the_client_sent_and_what_failed(self):
        client = mock.Mock(spec=['easy_apply', 'applied', 'unconfirmed', 'failed'])
        client.easy_apply.side_effect = KeyboardInterrupt
        client.applied = ['https://linkedin.example/1']
        client.unconfirmed = []
        client.failed = {'https://linkedin.example/2': 'TimeoutError'}
        with self.assertRaises(Interrupted) as stopped:
            easy_apply_with(client, [job('linkedin', 1), job('linkedin', 2), job('linkedin', 3)])
        self.assertEqual(
            [(result.job.id, result.status) for result in stopped.exception.results],
            [('linkedin-1', 'applied'), ('linkedin-2', 'failed')],
        )

    def test_a_crashing_client_names_its_error(self):
        client = mock.Mock(spec=['easy_apply'])
        client.easy_apply.side_effect = RuntimeError('browser died')
        with self.assertLogs('applicant.services.apply', 'ERROR'):
            (entry,) = easy_apply_with(client, [job('linkedin', 1)])
        self.assertEqual(entry[1:], ('failed', 'RuntimeError'))

    def test_a_dry_run_says_so(self):
        (result,) = EasyApply(lambda: self.fail('no client for a dry run')).apply(
            [job('linkedin', 1)], dry_run=True
        )
        self.assertEqual(result, ApplicationResult(job('linkedin', 1), 'would_apply', 'dry run'))

    def test_nothing_declined_is_an_empty_list(self):
        self.assertEqual(ApplyToJobs({}).declined, [])

    def test_a_plain_interrupt_still_saves_the_other_rows(self):
        applier = mock.Mock(source='linkedin')
        applier.apply.side_effect = KeyboardInterrupt
        log = str(self.root / 'a.csv')
        with (
            self.assertRaises(KeyboardInterrupt),
            self.assertLogs('applicant.services.apply', 'WARNING'),
        ):
            ApplyToJobs({'linkedin': applier}).run(
                [job('linkedin', 1), job('indeed', 2, company='Beta')], log=log, dry_run=False
            )
        self.assertEqual([row['id'] for row in ApplicationLog(log).rows()], ['indeed-2'])

    def test_how_many_were_already_settled_is_said(self):
        log = str(self.root / 'a.csv')
        applier = mock.Mock(source='linkedin')
        applier.apply.side_effect = lambda jobs, dry_run: [
            ApplicationResult(one, 'applied', 'ok') for one in jobs
        ]
        jobs = [job('linkedin', 1), job('linkedin', 2)]
        ApplyToJobs({'linkedin': applier}).run(jobs, log=log, dry_run=False)
        with self.assertLogs('applicant.services.apply', 'INFO') as logged:
            ApplyToJobs({'linkedin': applier}).run(jobs, log=log, dry_run=False)
        self.assertIn('apply: 2 job(s) already settled', '\n'.join(logged.output))

    def test_selecting_with_a_salary_floor_fetches_the_rates_once(self):
        table = mock.Mock()
        table.snapshot.return_value = RateSnapshot(market={'USD': 1.0, 'INR': 80.0})
        rule = JobFilter(min_salary=1_000_000, currency='INR', rates=table, salary_basis='market')
        kept = select([job(salary='$20,000 a year'), job(n=2, salary='$1,000 a year')], rule)
        self.assertEqual([one.id for one in kept], ['indeed-1'])
        table.snapshot.assert_called_once()


class FanOutEdgesTest(unittest.TestCase):
    def test_each_outcome_and_event_says_whose_and_what(self):
        events = []
        boom = RuntimeError('boom')

        def call(source):
            if source == 'b':
                raise Blocked('bot check')
            if source == 'c':
                raise boom
            return source.upper()

        with self.assertLogs('applicant.services.fanout', 'WARNING'):
            outcomes = fan_out(['a', 'b', 'c'], call, events.append)
        self.assertEqual([outcome.source for outcome in outcomes], ['a', 'b', 'c'])
        self.assertEqual(outcomes[0].result, 'A')
        self.assertEqual(
            [(e.source, e.expected) for e in events if isinstance(e, SourceFailed)],
            [('b', True), ('c', False)],
        )
        self.assertIs(events[-1].error, boom)


class FinancialsServiceEdgesTest(Folder):
    def test_companies_are_tracked_once_whatever_their_case(self):
        listing = self.root / 'jobs.json'
        from applicant.storage import save_jobs

        save_jobs([job(company='zomato'), job(n=2, company='Swiggy')], str(listing))
        names = financials_service.companies_to_track(['Zomato'], str(listing))
        self.assertEqual(names, ['Zomato', 'Swiggy'])

    def test_a_listing_spelling_a_company_two_ways_tracks_it_once(self):
        listing = self.root / 'jobs.json'
        from applicant.storage import save_jobs

        save_jobs([job(company='Swiggy'), job(n=2, company='SWIGGY')], str(listing))
        self.assertEqual(financials_service.companies_to_track([], str(listing)), ['Swiggy'])

    def test_the_round_limit_reaches_the_client_and_the_record_is_kept(self):
        from applicant.financials import CompanyFinancials

        client = mock.Mock(spec=['fetch'])
        client.fetch.return_value = CompanyFinancials(source='crunchbase', company='zomato')
        output = str(self.root / 'f.json')
        with self.assertLogs('applicant', 'INFO'):
            tracked = financials_service.track_financials(
                ['zomato'], {'crunchbase': client}, output, max_rounds=5, pause=0, backend='sqlite'
            )
        client.fetch.assert_called_once_with('zomato', max_rounds=5)
        self.assertEqual((tracked.companies, tracked.found, tracked.output), (1, 1, output))
        from applicant.infra.store.sqlite import Store

        with Store.beside(output) as kept:
            self.assertEqual(len(kept.financial_history(output, 'crunchbase:zomato')), 1)


class RatesServiceEdgesTest(unittest.TestCase):
    def test_a_factor_without_a_year_says_so_and_known_counts_only_the_held(self):
        with mock.patch.object(
            rates_service, 'load_factors', return_value={'SWE': {'value': 10.5}}
        ):
            table = rates_service.cached()
        sek = next(row for row in table.rows if row.currency == 'SEK')
        self.assertEqual((sek.value, sek.year), (10.5, '?'))
        self.assertEqual(table.known, 1)

    def test_refresh_passes_its_choices_on(self):
        with mock.patch.object(
            rates_service, 'refresh_factors', return_value=([], [], [])
        ) as fetch:
            done = rates_service.refresh(None, force=True, into='table.json')
        self.assertEqual(fetch.call_args.kwargs['force'], True)
        self.assertEqual(fetch.call_args.kwargs['path'], 'table.json')
        self.assertIsNone(fetch.call_args.kwargs['currencies'])
        self.assertEqual(done.path, 'table.json')


class ReviewsServiceEdgesTest(Folder):
    def test_the_request_reaches_the_client_and_the_answer_comes_back(self):
        from applicant.reviews.models import CompanyRating

        rating = CompanyRating(source='ambitionbox', company='TCS', url='u', overall_rating=3.8)
        client = mock.Mock(spec=['fetch'])
        client.fetch.return_value = rating
        events = []
        output = str(self.root / 'r.json')
        with self.assertLogs('applicant', 'INFO'):
            found = fetch_reviews(
                'tcs', {'ambitionbox': client}, output, max_reviews=7, emit=events.append
            )
        client.fetch.assert_called_once_with('tcs', max_reviews=7)
        self.assertEqual(events, [RatingFetched('ambitionbox', rating)])
        self.assertEqual((found.ratings, found.written_to), ([rating], output))

    def test_a_failing_site_is_reported_through_emit(self):
        from applicant.reviews.models import CompanyRating

        rating = CompanyRating(source='ambitionbox', company='TCS', url='u', overall_rating=3.8)
        working = mock.Mock(spec=['fetch'])
        working.fetch.return_value = rating
        refusing = mock.Mock(spec=['fetch'])
        refusing.fetch.side_effect = Blocked('bot check')
        events = []
        with self.assertLogs('applicant', 'WARNING'):
            fetch_reviews(
                'tcs',
                {'ambitionbox': working, 'glassdoor': refusing},
                str(self.root / 'r.json'),
                emit=events.append,
            )
        self.assertEqual(
            [event.source for event in events if isinstance(event, SourceFailed)], ['glassdoor']
        )


class SearchServiceEdgesTest(Folder):
    def test_store_reports_where_and_how_many_and_uses_the_store_asked_for(self):
        path = str(self.root / 'job_listing.json')
        stored = store([job(), job(n=2)], path, backend='sqlite')
        self.assertEqual((stored.path, stored.saved, stored.total), (path, 2, 2))
        self.assertTrue((self.root / 'applicant.db').exists())

    def board(self, pages, native=()):
        client = mock.Mock(spec=['search', 'close', 'capability'])
        client.capability = Capability(filters=frozenset(native))
        client.search.side_effect = pages
        return client

    def test_rounds_grow_until_enough_survive(self):
        wanted = JobFilter(title='python')
        pages = [
            [job(n=n, title='Chef') for n in range(5)],  # 5 read, none kept
            [job(n=n, title='Python Dev' if n < 3 else 'Chef') for n in range(10)],
        ]
        client = self.board(pages)
        events = []
        found = SearchJobs(lambda name: client).run(
            ['indeed'], 'dev', wanted, limit=5, want=2, max_rounds=3, emit=events.append
        )
        self.assertEqual(len(found), 2, 'cut to what was wanted')
        self.assertEqual([call.kwargs['limit'] for call in client.search.call_args_list], [5, 10])
        self.assertEqual(client.search.call_args.args[0], 'dev')
        self.assertEqual(events[-1], BoardSearched('indeed', 2, 10))

    def test_without_want_a_board_is_read_once(self):
        client = self.board([[job(n=n) for n in range(5)]] * 3)
        SearchJobs(lambda name: client).run(['indeed'], 'dev', JobFilter(title='nothing'), limit=5)
        self.assertEqual(client.search.call_count, 1)

    def test_the_last_round_does_not_ask_for_more(self):
        client = self.board(
            [
                [job(n=n, title='Chef') for n in range(5)],
                [job(n=n, title='Chef') for n in range(10)],
            ]
        )
        with self.assertLogs('applicant.services.search', 'INFO') as logged:
            SearchJobs(lambda name: client).run(
                ['indeed'], 'dev', JobFilter(title='python'), limit=5, want=1, max_rounds=2
            )
        self.assertEqual(
            sum('reading' in line for line in logged.output), 1, 'one growth for two rounds'
        )

    def test_enough_survivors_in_the_first_round_end_the_search(self):
        client = self.board([[job(n=n, title='Python Dev' if n < 2 else 'Chef') for n in range(5)]])
        found = SearchJobs(lambda name: client).run(
            ['indeed'], 'dev', JobFilter(title='python'), limit=5, want=2, max_rounds=3
        )
        self.assertEqual(len(found), 2)
        self.assertEqual(client.search.call_count, 1)

    def test_a_salary_floor_fetches_the_rates_each_page_needs(self):
        table = mock.Mock()
        table.snapshot.return_value = RateSnapshot(market={'USD': 1.0, 'INR': 80.0})
        rule = JobFilter(min_salary=1_000_000, currency='INR', rates=table, salary_basis='market')
        client = self.board([[job(salary='$20,000 a year')]])
        found = SearchJobs(lambda name: client).run(['indeed'], 'dev', rule)
        self.assertEqual(len(found), 1)
        self.assertEqual(table.snapshot.call_args.args[0], {'INR', 'USD'})

    def test_a_board_that_filters_dates_itself_is_asked_to(self):
        client = self.board([[job()]], native={Field.POSTED})
        SearchJobs(lambda name: client).run(['indeed'], 'dev', JobFilter(posted_within_days=3))
        self.assertEqual(client.search.call_args.kwargs['posted_within_days'], 3)

    def test_enrichment_reads_only_what_it_must_and_counts_each_page(self):
        known = job(n=1, experience_min=1, experience_max=3)
        unknown = [job(n=n) for n in (2, 3, 4)]
        client = mock.Mock()
        client.describe.side_effect = ['3-5 years', Blocked('slow down'), 'never asked']
        plan = Enrichment(budget=5)
        SearchJobs(lambda name: client)._enrich(client, [known, *unknown], plan)
        self.assertEqual(
            client.describe.call_count, 2, 'the known one skipped, stopped at the refusal'
        )
        self.assertTrue(plan.stopped)
        self.assertEqual(plan.budget, 4)
        self.assertEqual((unknown[0].experience_min, unknown[0].experience_max), (3.0, 5.0))

    def test_enrichment_goes_on_past_a_page_that_says_nothing(self):
        jobs = [job(n=1), job(n=2)]
        client = mock.Mock()
        client.describe.side_effect = ['a great team', '2-4 years']
        SearchJobs(lambda name: client)._enrich(client, jobs, Enrichment(budget=5))
        self.assertEqual((jobs[1].experience_min, jobs[1].experience_max), (2.0, 4.0))

    def test_a_page_already_read_is_used_once_the_budget_is_spent(self):
        jobs = [job(n=1), job(n=2), job(n=3)]
        plan = Enrichment(budget=1, described={('indeed', 'indeed-3'): '1-2 years'})
        client = mock.Mock()
        client.describe.return_value = 'nothing useful'
        SearchJobs(lambda name: client)._enrich(client, jobs, plan)
        self.assertEqual(client.describe.call_count, 1)
        self.assertEqual(jobs[2].experience_max, 2.0)

    def test_a_page_already_read_is_used_after_a_refusal(self):
        jobs = [job(n=1), job(n=2)]
        plan = Enrichment(budget=5, described={('indeed', 'indeed-2'): '1-2 years'})
        client = mock.Mock()
        client.describe.side_effect = Blocked('slow down')
        with self.assertLogs('applicant.services.search', 'WARNING'):
            SearchJobs(lambda name: client)._enrich(client, jobs, plan)
        self.assertTrue(plan.stopped)
        self.assertEqual(jobs[1].experience_max, 2.0)

    def test_the_held_back_experience_check_keeps_the_filter_s_policy(self):
        client = mock.Mock(spec=['search', 'close', 'capability', 'describe'])
        client.capability = Capability(filters=frozenset())
        client.search.return_value = [job()]
        client.describe.return_value = 'a great team'
        strict = JobFilter(experience=2, keep_unknown=False)
        found = SearchJobs(lambda name: client).run(['indeed'], 'dev', strict, enrich=True)
        self.assertEqual(found, [], 'still unknown after reading: dropped')
        found = SearchJobs(lambda name: client).run(
            ['indeed'], 'dev', JobFilter(experience=2), enrich=True
        )
        self.assertEqual(len(found), 1, 'kept, flagged, by default')

    def test_a_card_with_one_bound_is_not_read_again(self):
        client = mock.Mock()
        SearchJobs(lambda name: client)._enrich(
            client, [job(experience_min=2)], Enrichment(budget=5)
        )
        client.describe.assert_not_called()


class StatusEdgesTest(Folder):
    def test_the_summary_names_its_files(self):
        listing, log = str(self.root / 'l.json'), str(self.root / 'a.csv')
        with redirect_stderr(io.StringIO()), self.assertLogs('applicant', 'INFO'):
            status = summarise(listing, log)
        self.assertEqual((status.input, status.log), (listing, log))


if __name__ == '__main__':
    unittest.main()
