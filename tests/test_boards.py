"""The per-board parsers, against trimmed fixtures.

These are what break when a site redesigns, so the point of each test is to fail
informatively when a fixture goes stale rather than to prove the site still works.
No test here touches the network.
"""

from __future__ import annotations

import json
import unittest
from typing import ClassVar

import httpx

from applicant.boards.googlejobs import GoogleJobs
from applicant.boards.indeed import Indeed, host_for
from applicant.boards.linkedin import LinkedIn
from applicant.boards.naukri import Naukri, search_url
from applicant.models import BlockedError, Job


def mosaic_html(results: list[dict]) -> str:
    """Indeed ships its job cards as a JSON blob inside a <script>."""
    payload = {'metaData': {'mosaicProviderJobCardsModel': {'results': results}}}
    return (
        '<html><body><script>window.mosaic.providerData["mosaic-provider-jobcards"] = '
        + json.dumps(payload)
        + ';</script></body></html>'
    )


class IndeedHostTest(unittest.TestCase):
    def test_country_picks_the_host(self):
        """The US site quietly ignores l=India and answers with US jobs."""
        self.assertEqual(host_for('Bengaluru, India'), 'https://in.indeed.com')

    def test_aliases(self):
        self.assertEqual(host_for('London, UK'), 'https://uk.indeed.com')
        self.assertEqual(host_for('Dubai, UAE'), 'https://ae.indeed.com')

    def test_case_insensitive(self):
        self.assertEqual(host_for('INDIA'), 'https://in.indeed.com')

    def test_unknown_location_falls_back_to_the_us_site(self):
        self.assertEqual(host_for('Springfield'), 'https://www.indeed.com')
        self.assertEqual(host_for(''), 'https://www.indeed.com')
        self.assertEqual(host_for(None), 'https://www.indeed.com')

    def test_a_substring_is_not_a_country(self):
        # 'Indiana' must not resolve to the Indian site
        self.assertEqual(host_for('Indianapolis, Indiana'), 'https://www.indeed.com')


class IndeedUrlTest(unittest.TestCase):
    def setUp(self):
        self.client = Indeed(domain='https://www.indeed.com')

    def test_first_page_has_no_start(self):
        self.assertNotIn('start=', self.client._url('python', 'Pune', 0))

    def test_pagination_and_age_filter(self):
        url = self.client._url('python dev', 'Pune', 10, posted_within_days=7)
        self.assertIn('start=10', url)
        self.assertIn('fromage=7', url)
        self.assertIn('q=python+dev', url)


class IndeedParseTest(unittest.TestCase):
    ITEM: ClassVar[dict] = {
        'jobkey': 'abc123',
        'title': 'Python Developer',
        'company': 'Acme',
        'formattedLocation': 'Bengaluru, Karnataka',
        'link': '/rc/clk?jk=abc123',
        'pubDate': 1_754_179_200_000,
        'formattedRelativeTime': '6 days ago',
        'salarySnippet': {'text': '₹8,00,000 - ₹12,00,000 a year'},
        'jobTypes': ['Full-time'],
        'remoteLocation': True,
    }

    def setUp(self):
        self.client = Indeed(domain='https://in.indeed.com')

    def test_fields_are_mapped(self):
        job = self.client._to_job(self.ITEM)
        self.assertEqual(job.source, 'indeed')
        self.assertEqual(job.id, 'abc123')
        self.assertEqual(job.title, 'Python Developer')
        self.assertEqual(job.company, 'Acme')
        self.assertEqual(job.location, 'Bengaluru, Karnataka')
        self.assertEqual(job.employment_type, 'Full-time')
        self.assertTrue(job.remote)

    def test_relative_links_are_made_absolute(self):
        self.assertEqual(
            self.client._to_job(self.ITEM).url, 'https://in.indeed.com/rc/clk?jk=abc123'
        )

    def test_epoch_becomes_an_iso_date(self):
        self.assertEqual(self.client._to_job(self.ITEM).posted, '2025-08-03')

    def test_a_sparse_item_does_not_crash(self):
        job = self.client._to_job({'title': 'Dev'})
        self.assertEqual(job.title, 'Dev')
        self.assertIsNone(job.url)
        self.assertIsNone(job.salary)

    def test_employment_type_from_taxonomy_attributes(self):
        item = {
            'title': 'Dev',
            'taxonomyAttributes': [{'label': 'job-types', 'attributes': [{'label': 'Contract'}]}],
        }
        self.assertEqual(self.client._to_job(item).employment_type, 'Contract')

    def test_results_are_read_out_of_the_mosaic_blob(self):
        results = self.client._results(mosaic_html([self.ITEM]))
        self.assertEqual(len(results), 1)

    def test_a_page_without_the_blob_reads_as_blocked(self):
        with self.assertRaises(BlockedError):
            self.client._results('<html>Access Denied</html>')

    def test_a_malformed_blob_reads_as_blocked(self):
        html = (
            '<script>window.mosaic.providerData["mosaic-provider-jobcards"] = {not json};</script>'
        )
        with self.assertRaises(BlockedError):
            self.client._results(html)


class IndeedSearchTest(unittest.TestCase):
    """The whole search loop, over an injected transport - no network, no sleeping."""

    def items(self, start: int, count: int) -> list[dict]:
        return [
            {'jobkey': 'k{}'.format(start + n), 'title': 'Dev {}'.format(start + n)}
            for n in range(count)
        ]

    def client(self, handler) -> Indeed:
        transport = httpx.MockTransport(handler)
        return Indeed(
            domain='https://www.indeed.com', delay=0, client=httpx.Client(transport=transport)
        )

    def test_paginates_until_the_limit(self):
        def handler(request):
            start = int(dict(request.url.params).get('start', 0))
            return httpx.Response(200, text=mosaic_html(self.items(start, 10)))

        jobs = self.client(handler).search('python', 'Pune', limit=25)
        self.assertEqual(len(jobs), 25)
        self.assertEqual(jobs[0].id, 'k0')
        self.assertEqual(jobs[-1].id, 'k24')

    def test_stops_when_a_page_comes_back_empty(self):
        def handler(request):
            return httpx.Response(200, text=mosaic_html([]))

        self.assertEqual(self.client(handler).search('python', limit=25), [])

    def test_a_page_of_nothing_new_ends_the_loop(self):
        """Indeed clamps `start` and re-serves the same page; that must terminate."""
        calls = []

        def handler(request):
            calls.append(request.url)
            return httpx.Response(200, text=mosaic_html(self.items(0, 5)))

        jobs = self.client(handler).search('python', limit=25)
        self.assertEqual(len(jobs), 5)
        self.assertEqual(len(calls), 2, 'one page of results, one that added nothing')

    def test_partial_overlap_between_pages_still_advances(self):
        def handler(request):
            start = int(dict(request.url.params).get('start', 0))
            # pages overlap by half, as a busy board's paging often does
            return httpx.Response(200, text=mosaic_html(self.items(start // 2, 10)))

        jobs = self.client(handler).search('python', limit=12)
        self.assertEqual(len(jobs), 12)
        self.assertEqual(len({job.id for job in jobs}), 12)


class GoogleJobsParseTest(unittest.TestCase):
    """Google's class names rotate, so cards are classified line by line."""

    def setUp(self):
        self.client = GoogleJobs()

    def test_a_full_card(self):
        job = self.client._to_job(
            'A\nSenior Python Developer\nAcme Corp\n'
            'Bengaluru, India • via LinkedIn\n'
            '6 days ago\nFull-time\n₹20L–₹30L a year'
        )
        assert job is not None
        self.assertEqual(job.title, 'Senior Python Developer')
        self.assertEqual(job.company, 'Acme Corp')
        self.assertEqual(job.location, 'Bengaluru, India')
        self.assertEqual(job.via, 'LinkedIn')
        self.assertEqual(job.employment_type, 'Full-time')
        self.assertEqual(job.salary, '₹20L–₹30L a year')
        self.assertEqual(job.posted_text, '6 days ago')

    def test_the_logo_placeholder_initial_is_dropped(self):
        job = self.client._to_job('A\nDev\nAcme')
        assert job is not None
        self.assertEqual(job.title, 'Dev')

    def test_too_few_lines_is_not_a_job(self):
        self.assertIsNone(self.client._to_job('A\nDev'))
        self.assertIsNone(self.client._to_job(''))
        self.assertIsNone(self.client._to_job(None))

    def test_a_stable_id_is_derived_from_the_fields(self):
        text = 'Dev\nAcme\nPune • via LinkedIn'
        first, second = self.client._to_job(text), self.client._to_job(text)
        assert first is not None and second is not None
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(first.id or ''), 16)

    def test_different_postings_get_different_ids(self):
        one = self.client._to_job('Dev\nAcme\nPune • via LinkedIn')
        two = self.client._to_job('Dev\nOther\nPune • via LinkedIn')
        assert one is not None and two is not None
        self.assertNotEqual(one.id, two.id)

    def test_url_is_none_because_google_only_links_on_open(self):
        job = self.client._to_job('Dev\nAcme\nPune • via LinkedIn')
        assert job is not None
        self.assertIsNone(job.url)


class NaukriTest(unittest.TestCase):
    def test_search_url_slugs_the_query(self):
        self.assertEqual(
            search_url('Python Developer'), 'https://www.naukri.com/python-developer-jobs'
        )

    def test_search_url_with_a_location(self):
        self.assertEqual(
            search_url('python developer', 'Bengaluru'),
            'https://www.naukri.com/python-developer-jobs-in-bengaluru',
        )

    def test_search_url_pages(self):
        self.assertTrue(search_url('python', page=3).endswith('-3'))

    def test_punctuation_is_stripped_from_the_slug(self):
        self.assertEqual(
            search_url('C++ / .NET Developer'), 'https://www.naukri.com/c-net-developer-jobs'
        )

    def test_api_job_mapping(self):
        job = Naukri()._api_job(
            {
                'jobId': 12345,
                'title': '  Python Developer  ',
                'companyName': 'Acme',
                'jdURL': '/job-listings-python-developer-acme-12345',
                'placeholders': [
                    {'type': 'location', 'label': 'Pune'},
                    {'type': 'salary', 'label': '5-8 Lacs PA'},
                    {'type': 'experience', 'label': '2-5 Yrs'},
                ],
                'footerPlaceholderLabel': '3 days ago',
            }
        )
        assert job is not None
        assert job.url is not None
        self.assertEqual(job.id, '12345')
        self.assertEqual(job.title, 'Python Developer')
        self.assertEqual(job.location, 'Pune')
        self.assertEqual(job.salary, '5-8 Lacs PA')
        # Naukri's "2-5 Yrs" is required experience, not an employment type
        self.assertIsNone(job.employment_type)
        self.assertEqual(job.experience_text, '2-5 Yrs')
        self.assertEqual((job.experience_min, job.experience_max), (2.0, 5.0))
        self.assertTrue(job.url.startswith('https://www.naukri.com/'))

    def test_a_titleless_item_is_skipped(self):
        self.assertIsNone(Naukri()._api_job({'jobId': 1}))

    def test_not_disclosed_salary_becomes_unknown(self):
        self.assertIsNone(Naukri()._salary('Not disclosed'))
        self.assertIsNone(Naukri()._salary(None))
        self.assertEqual(Naukri()._salary('5-8 Lacs PA'), '5-8 Lacs PA')

    def test_id_recovered_from_a_url_when_the_attribute_is_missing(self):
        self.assertEqual(
            Naukri()._id_from_url('/job-listings-python-developer-acme-140825001234'),
            '140825001234',
        )
        self.assertIsNone(Naukri()._id_from_url('/no-digits-here'))


class LinkedInGuestCardTest(unittest.TestCase):
    CARD = """
      <div class="base-card" data-entity-urn="urn:li:jobPosting:3812345678">
        <a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/dev-at-acme-3812345678?refId=x">
        <h3 class="base-search-card__title">
            Senior Python Developer
        </h3>
        <a class="hidden-nested-link" href="/company/acme">
            Acme &amp; Co
        </a>
        <span class="job-search-card__location">Bengaluru, Karnataka, India</span>
        <time datetime="2026-07-28">1 week ago</time>
      </div>
    """

    def setUp(self):
        self.client = LinkedIn()
        self.addCleanup(self.client.close)

    def test_fields_are_extracted(self):
        job = self.client._card_to_job(self.CARD)
        assert job is not None
        self.assertEqual(job.source, 'linkedin')
        self.assertEqual(job.id, '3812345678')
        self.assertEqual(job.title, 'Senior Python Developer')
        self.assertEqual(job.location, 'Bengaluru, Karnataka, India')
        self.assertEqual(job.posted, '2026-07-28')

    def test_entities_are_decoded_and_tags_stripped(self):
        job = self.client._card_to_job(self.CARD)
        assert job is not None
        self.assertEqual(job.company, 'Acme & Co')

    def test_query_strings_are_dropped_from_the_link(self):
        job = self.client._card_to_job(self.CARD)
        assert job is not None
        self.assertEqual(job.url, 'https://www.linkedin.com/jobs/view/dev-at-acme-3812345678')

    def test_a_card_without_a_title_is_skipped(self):
        self.assertIsNone(self.client._card_to_job('<div>nothing useful</div>'))

    def test_easy_apply_is_unknown_from_a_guest_card(self):
        job = self.client._card_to_job(self.CARD)
        assert job is not None
        self.assertIsNone(job.easy_apply)


class LinkedInGuestSearchTest(unittest.TestCase):
    def card(self, job_id: str) -> str:
        return (
            '<li><div data-entity-urn="urn:li:jobPosting:{0}">'
            '<h3 class="base-search-card__title">Dev {0}</h3></div></li>'.format(job_id)
        )

    def client(self, handler) -> LinkedIn:
        instance = LinkedIn(delay=0, client=httpx.Client(transport=httpx.MockTransport(handler)))
        self.addCleanup(instance.close)
        return instance

    def test_paginates_until_the_limit(self):
        def handler(request):
            start = int(dict(request.url.params).get('start', 0))
            return httpx.Response(200, text=''.join(self.card(str(start + n)) for n in range(10)))

        jobs = self.client(handler).search('python', limit=25)
        self.assertEqual(len(jobs), 25)

    def test_a_page_of_nothing_new_ends_the_loop(self):
        """The guest endpoint re-serves the last page past the end of the results."""

        def handler(request):
            return httpx.Response(200, text=''.join(self.card(str(n)) for n in range(5)))

        jobs = self.client(handler).search('python', limit=25)
        self.assertEqual(len(jobs), 5)

    def test_rate_limiting_is_reported_not_swallowed(self):
        def handler(request):
            return httpx.Response(429)

        with self.assertRaises(BlockedError):
            self.client(handler).search('python', limit=5)

    def test_the_age_filter_is_sent_in_seconds(self):
        seen = []

        def handler(request):
            seen.append(dict(request.url.params))
            return httpx.Response(200, text='')

        self.client(handler).search('python', limit=5, posted_within_days=7)
        self.assertEqual(seen[0]['f_TPR'], 'r604800')


class JobValidationTest(unittest.TestCase):
    """Job is a Pydantic model, so postings are normalised as they are built."""

    def test_whitespace_is_stripped(self):
        self.assertEqual(Job(source='indeed', title='  Dev  ').title, 'Dev')

    def test_a_blank_string_becomes_none(self):
        """Boards emit empty cells constantly; '' is absence, not a value."""
        job = Job(source='indeed', title='Dev', company='', location='   ')
        self.assertIsNone(job.company)
        self.assertIsNone(job.location)

    def test_a_missing_title_is_rejected(self):
        with self.assertRaises(ValueError):
            Job(source='indeed')  # type: ignore[call-arg]

    def test_flags_default_to_a_fresh_list_per_job(self):
        first = Job(source='indeed', title='A')
        first.flags.append('salary-unknown')
        self.assertEqual(Job(source='indeed', title='B').flags, [])

    def test_round_trips_through_to_dict(self):
        job = Job(source='naukri', title='Dev', experience_text='2-5 Yrs', salary='5-8 Lacs PA')
        restored = Job.from_dict(job.to_dict())
        self.assertEqual(restored.experience_min, 2.0)
        self.assertEqual(restored.salary, '5-8 Lacs PA')

    def test_from_dict_ignores_fields_we_no_longer_know(self):
        job = Job.from_dict({'source': 'indeed', 'title': 'Dev', 'retired_field': 'x'})
        self.assertEqual(job.title, 'Dev')


if __name__ == '__main__':
    unittest.main()
