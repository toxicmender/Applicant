"""Can the filters actually find a specific shortlist of jobs?

The shortlist is eight AI/ML postings in Indian metros asking for one to five
years, the sort of thing someone would hand the tool and expect back. These
tests run that shortlist - plus postings that must *not* survive - through the
real `JobFilter`, the `Jobs` facade and the `search`/`apply` commands, and pin
down what each flag can and cannot express.

Nothing here touches the network: the boards are replaced by a stub returning
fixed postings, and no filter under test needs a currency conversion.
"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from applicant.boards import capability
from applicant.cli import main
from applicant.filters import JobFilter
from applicant.models import Job
from applicant.search import Jobs
from applicant.storage import load_jobs, save_jobs

# The shortlist, written the way a board hands it over: a title, the hiring
# company, a city with its state, and the experience string in the form each
# board emits (Naukri's '2-5 Yrs', an en dashed '2-5 years' elsewhere).
SHORTLIST = (
    ('naukri', 'Amazon', 'Associate Consultant - AI/ML', 'Bengaluru, Karnataka', '2-5 Yrs'),
    ('naukri', 'LPL Financial', 'Engineer I - AI Engineering', 'Hyderabad, Telangana', '2–4 years'),
    ('naukri', 'Walmart Global Tech', 'Data Scientist I', 'Bengaluru, Karnataka', '2-4 Yrs'),
    ('naukri', 'Mastercard', 'AI Engineer', 'Pune, Maharashtra', '1-5 Yrs'),
    ('naukri', 'Google', 'Forward Deployment AI Engineer', 'Hyderabad, Telangana', '2-5 years'),
    ('naukri', 'JPMorgan Chase & Co.', 'Associate AI Engineer', 'Mumbai, Maharashtra', '2-4 Yrs'),
    ('naukri', 'Cohere Health', 'Machine Learning Engineer', 'Hyderabad, Telangana', '2–5 years'),
    (
        'naukri',
        'Deepwatch India',
        'ML Engineer - Cybersecurity Automation',
        'Bengaluru, Karnataka',
        '2-4 Yrs',
    ),
)

# Postings the same search would drag in, and that a working filter has to shed:
# too junior, too senior, the wrong field, and the right job in the wrong country.
DECOYS = (
    ('naukri', 'Infosys', 'Graduate Trainee - AI', 'Bengaluru, Karnataka', 'Fresher'),
    ('naukri', 'Adobe', 'Principal Machine Learning Engineer', 'Bengaluru, Karnataka', '8-12 Yrs'),
    ('naukri', 'Zoho', 'Frontend Developer', 'Chennai, Tamil Nadu', '2-4 Yrs'),
    ('linkedin', 'Stripe', 'AI Engineer', 'Dublin, Ireland', '2-5 years'),
)


def posting(source, company, title, location, experience, **extra) -> Job:
    return Job(
        source=source,
        id='{}-{}'.format(source, abs(hash((company, title)))),
        company=company,
        title=title,
        location=location,
        experience_text=experience,
        url='https://{}.example/{}'.format(source, abs(hash(title))),
        **extra,
    )


def shortlist() -> list[Job]:
    return [posting(*row) for row in SHORTLIST]


def pool() -> list[Job]:
    """The shortlist as it would arrive: mixed in with jobs that must be dropped."""
    return shortlist() + [posting(*row) for row in DECOYS]


def titles(jobs) -> list[str]:
    return [job.title for job in jobs]


def kept(filters: JobFilter, jobs, **kwargs) -> list[Job]:
    return [job for job in jobs if filters.matches(job, **kwargs)[0]]


class StubBoard:
    """A board that answers with fixed postings, so no scrape and no network.

    It carries the real board's capability declaration and honours it: the one
    thing it filters is location, the way the real boards do, and a country
    search comes back as bare city names.
    """

    def __init__(self, jobs, name='naukri'):
        self.jobs = jobs
        self.name = name
        self.capability = capability(name)
        self.calls: list[tuple] = []
        self.closed = False

    def search(self, keywords, location='', limit=25, posted_within_days=None):
        self.calls.append((keywords, location, limit, posted_within_days))
        found = [job for job in self.jobs if job.source == self.name]
        if location and location.lower() not in ('india',):
            found = [job for job in found if location.lower() in (job.location or '').lower()]
        elif location:
            # 'India' is a country the board understands; the Irish posting is not in it
            found = [job for job in found if 'ireland' not in (job.location or '').lower()]
        return found[:limit]

    def close(self):
        self.closed = True


class ExperienceRangeTest(unittest.TestCase):
    """The one filter that separates this shortlist cleanly from its decoys."""

    def test_every_posting_yields_the_range_it_asked_for(self):
        expected = [(2, 5), (2, 4), (2, 4), (1, 5), (2, 5), (2, 4), (2, 5), (2, 4)]
        for job, (low, high) in zip(shortlist(), expected, strict=True):
            with self.subTest(job=job.title):
                self.assertEqual((job.experience_min, job.experience_max), (low, high))

    def test_an_en_dash_range_parses_like_a_hyphen(self):
        """Boards emit both, and '2-4 years' must not read as unknown."""
        hyphen = posting('naukri', 'X', 'AI Engineer', 'Pune, Maharashtra', '2-4 Yrs')
        en_dash = posting('naukri', 'X', 'AI Engineer', 'Pune, Maharashtra', '2–4 years')
        self.assertEqual(
            (hyphen.experience_min, hyphen.experience_max),
            (en_dash.experience_min, en_dash.experience_max),
        )

    def test_three_years_of_experience_keeps_the_whole_shortlist(self):
        self.assertEqual(len(kept(JobFilter(experience=3), shortlist())), len(SHORTLIST))

    def test_it_sheds_the_fresher_and_the_principal_role(self):
        survivors = titles(kept(JobFilter(experience=3), pool()))
        self.assertNotIn('Graduate Trainee - AI', survivors)
        self.assertNotIn('Principal Machine Learning Engineer', survivors)

    def test_the_edges_of_the_shortlist_are_where_it_starts_thinning(self):
        """Documented behaviour: your years must fall *inside* the stated range.

        So one year only clears the 1-5 posting, and five years is outside every
        2-4 one. Someone with two to four years is who this shortlist is for.
        """
        counts = {years: len(kept(JobFilter(experience=years), shortlist())) for years in range(7)}
        self.assertEqual(counts, {0: 0, 1: 1, 2: 8, 3: 8, 4: 8, 5: 4, 6: 0})

    def test_a_board_that_never_publishes_experience_says_so(self):
        """Only Naukri publishes it, so the rest survive flagged as unpublished."""
        silent = posting('linkedin', 'Google', 'AI Engineer', 'Hyderabad, Telangana', None)
        keep, flags = JobFilter(experience=3).matches(silent)
        self.assertTrue(keep)
        self.assertEqual(flags, ['experience-unpublished'])

    def test_a_naukri_posting_that_states_nothing_is_merely_unknown(self):
        """Same silence, different source: this board could have told us."""
        silent = posting('naukri', 'Google', 'AI Engineer', 'Hyderabad, Telangana', None)
        keep, flags = JobFilter(experience=3).matches(silent)
        self.assertTrue(keep)
        self.assertEqual(flags, ['experience-unknown'])

    def test_strict_drops_both_kinds_of_silence(self):
        for source in ('linkedin', 'naukri'):
            with self.subTest(source=source):
                silent = posting(source, 'Google', 'AI Engineer', 'Hyderabad', None)
                keep, _ = JobFilter(experience=3, keep_unknown=False).matches(silent)
                self.assertFalse(keep)

    def test_keeping_the_unpublished_spares_the_boards_that_never_say(self):
        """What `--strict-published` is for: tighten Naukri, keep the other three."""
        strict = JobFilter(experience=3, keep_unknown=False, keep_unpublished=True)

        keep, flags = strict.matches(posting('linkedin', 'G', 'AI Engineer', 'Pune', None))
        self.assertTrue(keep, 'LinkedIn never publishes experience; that is not the job hiding')
        self.assertEqual(flags, ['experience-unpublished'])

        keep, _ = strict.matches(posting('naukri', 'G', 'AI Engineer', 'Pune', None))
        self.assertFalse(keep, 'Naukri does publish it, and this posting did not')

    def test_the_shortlist_is_unaffected_because_it_states_its_range(self):
        strict = JobFilter(experience=3, keep_unknown=False, keep_unpublished=True)
        self.assertEqual(len(kept(strict, shortlist())), len(SHORTLIST))


class TitleFilterTest(unittest.TestCase):
    """`--title` is one AND-of-words string, which this shortlist outgrows."""

    def test_no_single_title_filter_reaches_the_whole_shortlist(self):
        for wanted in ('ai', 'ai engineer', 'engineer', 'machine learning', 'ml', 'ai ml'):
            with self.subTest(title=wanted):
                self.assertLess(len(kept(JobFilter(title=wanted), shortlist())), len(SHORTLIST))

    def test_engineer_is_the_best_single_pass_and_still_misses_two(self):
        missed = set(titles(shortlist())) - set(
            titles(kept(JobFilter(title='engineer'), shortlist()))
        )
        self.assertEqual(missed, {'Associate Consultant - AI/ML', 'Data Scientist I'})

    def test_the_shortlist_needs_a_pass_per_title_family(self):
        """Four runs cover it, because there is no way to say 'ai OR ml' in one."""
        found: set[str] = set()
        for wanted in ('ai', 'ml', 'machine learning', 'data scientist'):
            found.update(titles(kept(JobFilter(title=wanted), shortlist())))
        self.assertEqual(found, set(titles(shortlist())))

    def test_all_the_words_must_appear_so_word_order_does_not_matter(self):
        job = posting(
            'naukri', 'Google', 'Forward Deployment AI Engineer', 'Hyderabad', '2-5 years'
        )
        self.assertTrue(JobFilter(title='engineer ai').matches(job)[0])
        self.assertFalse(JobFilter(title='ai architect').matches(job)[0], 'architect is absent')

    def test_matching_is_on_substrings_so_short_words_overreach(self):
        """'ai' is inside 'Trainee', so a two letter filter pulls in a fresher role.

        Worth knowing before reaching for `-t ai`: pair it with `-e` or use a
        longer word.
        """
        trainee = posting('naukri', 'Infosys', 'Graduate Trainee - AI', 'Bengaluru', 'Fresher')
        retail = posting('indeed', 'Reliance', 'Graduate Trainee - Retail', 'Mumbai', 'Fresher')
        self.assertTrue(JobFilter(title='ai').matches(trainee)[0])
        self.assertTrue(JobFilter(title='ai').matches(retail)[0], 'no AI in it at all')


class CompanyFilterTest(unittest.TestCase):
    def test_each_company_on_the_shortlist_is_reachable_by_name(self):
        for job in shortlist():
            with self.subTest(company=job.company):
                first_word = (job.company or '').split()[0]
                self.assertEqual(titles(kept(JobFilter(company=first_word), pool())), [job.title])

    def test_a_punctuated_name_matches_without_the_punctuation(self):
        """'JPMorgan Chase & Co.' is reachable as 'jpmorgan chase'."""
        keep, _ = JobFilter(company='jpmorgan chase').matches(shortlist()[5])
        self.assertTrue(keep)

    def test_company_and_experience_combine(self):
        survivors = kept(JobFilter(company='google', experience=3), pool())
        self.assertEqual(titles(survivors), ['Forward Deployment AI Engineer'])


class LocationFilterTest(unittest.TestCase):
    """Cities work locally; a country only works when the board did the filtering."""

    def test_a_city_selects_its_postings(self):
        self.assertEqual(len(kept(JobFilter(location='Bengaluru'), shortlist())), 3)
        self.assertEqual(len(kept(JobFilter(location='Hyderabad'), shortlist())), 3)

    def test_a_state_works_too_because_boards_include_it(self):
        self.assertEqual(len(kept(JobFilter(location='Karnataka'), shortlist())), 3)

    def test_india_recognises_its_own_cities(self):
        """Not one posting says 'India'; all eight are in it all the same."""
        self.assertEqual(len(kept(JobFilter(location='India'), shortlist())), len(SHORTLIST))

    def test_a_posting_somewhere_else_is_still_dropped(self):
        elsewhere = posting('linkedin', 'Stripe', 'AI Engineer', 'Dublin, Ireland', '2-5 years')
        keep, flags = JobFilter(location='India').matches(elsewhere)
        self.assertFalse(keep)
        self.assertEqual(flags, [])

    def test_a_place_we_cannot_resolve_is_kept_and_flagged(self):
        """The codebase's rule for anything it cannot check: keep it, say so."""
        vague = posting('linkedin', 'Acme', 'AI Engineer', 'Remote', '2-5 years')
        keep, flags = JobFilter(location='India').matches(vague)
        self.assertTrue(keep)
        self.assertEqual(flags, ['location-unverified'])

    def test_strict_drops_what_it_could_not_place(self):
        vague = posting('linkedin', 'Acme', 'AI Engineer', 'Remote', '2-5 years')
        keep, _ = JobFilter(location='India', keep_unknown=False).matches(vague)
        self.assertFalse(keep)

    def test_skipping_the_recheck_keeps_what_the_board_returned(self):
        survivors = kept(JobFilter(location='India'), shortlist(), skip=['location'])
        self.assertEqual(len(survivors), len(SHORTLIST))

    def test_naming_every_city_takes_one_pass_each(self):
        found: set[str] = set()
        for city in ('Bengaluru', 'Hyderabad', 'Pune', 'Mumbai'):
            found.update(titles(kept(JobFilter(location=city), shortlist())))
        self.assertEqual(found, set(titles(shortlist())))


class CombinedFilterTest(unittest.TestCase):
    """The filter a real run would carry, over the shortlist plus its decoys."""

    def test_a_realistic_search_returns_the_shortlist_and_nothing_else(self):
        survivors = kept(
            JobFilter(title='engineer', experience=3),
            [job for job in pool() if 'ireland' not in (job.location or '').lower()],
        )
        self.assertEqual(
            set(titles(survivors)),
            {
                'Engineer I - AI Engineering',
                'AI Engineer',
                'Forward Deployment AI Engineer',
                'Associate AI Engineer',
                'Machine Learning Engineer',
                'ML Engineer - Cybersecurity Automation',
            },
            'the two non-engineer titles need their own pass',
        )

    def test_the_union_of_four_passes_is_the_shortlist_exactly(self):
        """What it takes to get all eight and none of the decoys in one sitting."""
        survivors: dict[str, Job] = {}
        for wanted in ('ai', 'ml', 'machine learning', 'data scientist'):
            for job in kept(
                JobFilter(title=wanted, experience=3, location='India'), pool(), skip=['location']
            ):
                survivors[job.title] = job

        self.assertEqual(set(survivors), set(titles(shortlist())))
        self.assertNotIn('Graduate Trainee - AI', survivors, 'the fresher role is shed by -e 3')


class FacadeSearchTest(unittest.TestCase):
    """`Jobs.search`: location goes to the board, the rest is applied locally."""

    def stub(self, **kwargs):
        board = StubBoard(pool())
        return board, Jobs(sources=['naukri'], **kwargs)

    def run_search(self, board, jobs, filters):
        with patch.object(Jobs, '_client', return_value=board), redirect_stdout(io.StringIO()):
            return jobs.search('ai ml engineer', filters, limit=25)

    def test_location_is_pushed_to_the_board_not_rechecked(self):
        board, jobs = self.stub()
        found = self.run_search(board, jobs, JobFilter(location='India', experience=3))

        self.assertEqual(board.calls[0][1], 'India', 'the board was asked for India')
        # every shortlisted posting comes back, even though not one of them says
        # 'India' - the local recheck that would have dropped them all is skipped
        self.assertTrue(set(titles(shortlist())) <= set(titles(found)))
        # the decoys that location and experience *can* speak to are gone; the
        # frontend role is not, because nothing here filters on the field
        for gone in ('Graduate Trainee - AI', 'Principal Machine Learning Engineer'):
            self.assertNotIn(gone, titles(found))
        self.assertIn('Frontend Developer', titles(found), 'needs -t to shed')

    def test_the_experience_filter_still_runs_locally(self):
        board, jobs = self.stub()
        found = self.run_search(board, jobs, JobFilter(location='India'))
        self.assertIn('Graduate Trainee - AI', titles(found), 'no -e, so the fresher survives')

        found = self.run_search(board, jobs, JobFilter(location='India', experience=3))
        self.assertNotIn('Graduate Trainee - AI', titles(found))

    def test_a_city_search_narrows_to_that_city(self):
        board, jobs = self.stub()
        found = self.run_search(board, jobs, JobFilter(location='Hyderabad', experience=3))
        self.assertEqual(
            set(titles(found)),
            {
                'Engineer I - AI Engineering',
                'Forward Deployment AI Engineer',
                'Machine Learning Engineer',
            },
        )

    def test_the_date_filter_is_not_pushed_to_a_board_that_cannot_do_it(self):
        """Naukri's age filter sits behind an API we cannot call, so it stays local."""
        board, jobs = self.stub()
        self.run_search(board, jobs, JobFilter(location='India', posted_within_days=7))
        self.assertIsNone(board.calls[0][3])

    def test_undated_postings_survive_flagged_rather_than_vanishing(self):
        board, jobs = self.stub()
        found = self.run_search(board, jobs, JobFilter(location='India', posted_within_days=7))
        self.assertEqual(len(found), len(pool()) - 1, 'only the Irish posting is gone')
        for job in found:
            with self.subTest(job=job.title):
                self.assertIn('date-unknown', job.flags)


class SearchCommandTest(unittest.TestCase):
    """The same run through `applicant search`, argv and output file included."""

    def setUp(self):
        self._dir = TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.output = str(Path(self._dir.name) / 'job_listing.json')

    def run_cli(self, *argv):
        board = StubBoard(pool())
        buffer = io.StringIO()
        with patch.object(Jobs, '_client', return_value=board), redirect_stdout(buffer):
            code = main([*argv, '-o', self.output])
        return code, buffer.getvalue()

    def test_a_full_invocation_stores_the_shortlist(self):
        code, output = self.run_cli(
            'search', 'ai ml engineer', '-s', 'naukri', '-l', 'India', '-e', '3'
        )
        self.assertEqual(code, 0)
        # 11 postings on this board, 9 survive: the eight wanted, plus a frontend
        # role that only a -t pass can shed
        self.assertIn('naukri: 9 of 11 jobs match', output)
        self.assertTrue(set(titles(shortlist())) <= set(titles(load_jobs(self.output))))

    def test_the_title_flag_narrows_it_further(self):
        code, _ = self.run_cli(
            'search',
            'ai ml engineer',
            '-s',
            'naukri',
            '-l',
            'India',
            '-e',
            '3',
            '-t',
            'data scientist',
        )
        self.assertEqual(code, 0)
        self.assertEqual(titles(load_jobs(self.output)), ['Data Scientist I'])

    def test_runs_merge_rather_than_overwrite_so_passes_accumulate(self):
        """Four title passes into one file is how the whole shortlist is collected."""
        for wanted in ('ai', 'ml', 'machine learning', 'data scientist'):
            code, _ = self.run_cli(
                'search', 'ai ml engineer', '-s', 'naukri', '-l', 'India', '-e', '3', '-t', wanted
            )
            self.assertEqual(code, 0)

        stored = load_jobs(self.output)
        self.assertEqual(set(titles(stored)), set(titles(shortlist())))
        self.assertEqual(len(stored), len(SHORTLIST), 'no posting is stored twice')

    def run_one(self, source, *extra):
        """One posting that states no experience, from `source`, through the CLI."""
        board = StubBoard([posting(source, 'Google', 'AI Engineer', 'Hyderabad', None)], source)
        buffer = io.StringIO()
        with patch.object(Jobs, '_client', return_value=board), redirect_stdout(buffer):
            code = main(
                ['search', 'ai', '-s', source, '-l', 'India', '-e', '3', *extra, '-o', self.output]
            )
        return code, buffer.getvalue()

    def test_strict_would_drop_a_board_that_states_no_experience(self):
        code, output = self.run_one('naukri', '--strict')
        self.assertEqual(code, 1)
        self.assertIn('nothing matched', output)

    def test_strict_also_deletes_the_boards_that_never_state_it(self):
        """The behaviour --strict-published exists to give an alternative to."""
        code, _ = self.run_one('linkedin', '--strict')
        self.assertEqual(code, 1)

    def test_strict_published_keeps_the_board_that_never_states_it(self):
        code, _ = self.run_one('linkedin', '--strict-published')
        self.assertEqual(code, 0)
        self.assertEqual(load_jobs(self.output)[0].flags, ['experience-unpublished'])

    def test_strict_published_still_drops_a_board_that_could_have_said(self):
        code, _ = self.run_one('naukri', '--strict-published')
        self.assertEqual(code, 1)

    def test_strict_wins_when_both_are_given(self):
        code, _ = self.run_one('linkedin', '--strict', '--strict-published')
        self.assertEqual(code, 1)


class ApplyCommandTest(unittest.TestCase):
    """`apply` re-filters the stored file, and has no board to delegate to."""

    def setUp(self):
        self._dir = TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        root = Path(self._dir.name)
        self.listing = str(root / 'job_listing.json')
        self.log = str(root / 'applied_jobs.csv')
        save_jobs(pool(), self.listing)

    def run_cli(self, *filters):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(['apply', '-i', self.listing, '--log', self.log, '--dry-run', *filters])
        return code, buffer.getvalue()

    def test_experience_selects_the_shortlist_out_of_the_stored_file(self):
        code, output = self.run_cli('-e', '3')
        self.assertEqual(code, 0)
        # the eight wanted, plus the two decoys experience alone cannot judge:
        # a frontend role and an Irish one, both asking 2-4 years
        self.assertIn('10 of 12 stored jobs match', output)

    def test_a_city_narrows_the_stored_file(self):
        code, output = self.run_cli('-e', '3', '-l', 'Bengaluru')
        self.assertEqual(code, 0)
        self.assertIn('3 of 12 stored jobs match', output)

    def test_location_india_narrows_the_stored_file(self):
        """`apply` has no board to delegate to, so it resolves the country itself.

        Nine survive: the eight wanted plus an Indian frontend role, with the
        Irish posting the only one the country filter sheds.
        """
        code, output = self.run_cli('-e', '3', '-l', 'India')
        self.assertEqual(code, 0)
        self.assertIn('9 of 12 stored jobs match', output)

    def test_every_shortlisted_job_is_logged_for_manual_apply(self):
        """None of the shortlist is LinkedIn, so the CSV is a worklist of urls."""
        from applicant.storage import ApplicationLog

        code, _ = self.run_cli('-e', '3', '-t', 'engineer')
        self.assertEqual(code, 0)
        rows = ApplicationLog(self.log).rows()
        by_source = {row['source']: row['status'] for row in rows}
        self.assertEqual(by_source['naukri'], 'needs_manual_apply', 'hands off to the employer')
        self.assertEqual(by_source['linkedin'], 'would_apply', 'the Irish decoy, on a dry run')
        self.assertIn('Machine Learning Engineer', [row['title'] for row in rows])

    def test_the_flags_recorded_say_what_could_not_be_checked(self):
        from applicant.storage import ApplicationLog

        code, _ = self.run_cli('-e', '3', '-c', 'mastercard', '--min-salary', '1200000')
        self.assertEqual(code, 0)
        rows = ApplicationLog(self.log).rows()
        self.assertEqual(len(rows), 1)
        self.assertIn('salary-unknown', rows[0]['flags'])


if __name__ == '__main__':
    unittest.main()
