"""applicant.domain: pure, and held to it.

The layer rule is checked by reading the source, not trusted to review: the
domain imports no HTTP, browser, file or database library and nothing from the
layers above it, and never opens a file.
"""

from __future__ import annotations

import ast
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import httpx

from applicant.boards import CAPABILITIES, Field
from applicant.boards.googlejobs import GoogleJobs
from applicant.boards.indeed import Indeed
from applicant.boards.linkedin import LinkedInGuest
from applicant.boards.naukri import Naukri
from applicant.domain import dedupe, flags
from applicant.domain.capability import Capability
from applicant.domain.filtering import (
    FilterContext,
    JobFilter,
    PostedCheck,
    SalaryCheck,
    SilencePolicy,
    TextCheck,
)
from applicant.domain.job import Job
from applicant.domain.rates import EMPTY, RateSnapshot, convert
from applicant.filters import JobFilter as WiredJobFilter
from applicant.filters import prepared
from applicant.money import Rates
from tests.test_boards import IndeedParseTest, LinkedInGuestCardTest

DOMAIN = Path(__file__).resolve().parent.parent / 'src' / 'applicant' / 'domain'

# what the domain may not import: I/O libraries, and every layer above it
FORBIDDEN = {
    'httpx',
    'playwright',
    'sqlite3',
    'csv',
    'socket',
    'subprocess',
    'shutil',
    'tempfile',
    'pathlib',
    'os',
    'io',
    'urllib.request',
    'http',
    'applicant.infra',
    'applicant.boards',
    'applicant.reviews',
    'applicant.financials',
    'applicant.search',
    'applicant.cli',
    'applicant.services',
    'applicant.interaction',
    'applicant.storage',
    'applicant.files',
    'applicant.money',
    'applicant.filters',
    'applicant.log',
}


def imports_of(path: Path) -> set[str]:
    """Every module a file imports, relative imports resolved to absolute."""
    package = 'applicant.domain'
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.rsplit('.', node.level - 1)[0] if node.level > 1 else package
                found.add(f'{base}.{node.module}' if node.module else base)
            else:
                found.add(node.module or '')
    return found


class LayerRuleTest(unittest.TestCase):
    def modules(self) -> list[Path]:
        found = sorted(DOMAIN.glob('*.py'))
        self.assertTrue(found, 'no domain modules found')
        return found

    def test_the_domain_imports_no_io_and_nothing_above_it(self):
        for path in self.modules():
            for name in imports_of(path):
                with self.subTest(module=path.name, imports=name):
                    self.assertFalse(
                        any(name == bad or name.startswith(bad + '.') for bad in FORBIDDEN),
                        f'{path.name} imports {name}',
                    )

    def test_the_domain_never_opens_a_file(self):
        for path in self.modules():
            tree = ast.parse(path.read_text(encoding='utf-8'))
            calls = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == 'open'
            ]
            with self.subTest(module=path.name):
                self.assertEqual(calls, [], f'{path.name} calls open()')

    def test_the_rule_would_catch_a_relative_import_upward(self):
        """The checker itself: `from ..money import x` must resolve to applicant.money."""
        with tempfile.TemporaryDirectory() as folder:
            probe = Path(folder) / 'probe.py'
            probe.write_text('from ..money import Rates\nfrom . import flags\n', encoding='utf-8')
            self.assertEqual(imports_of(probe), {'applicant.money', 'applicant.domain'})


# -- capabilities, checked against what the parsers actually produce --------


def parsed_samples() -> dict[str, Job]:
    """One job per board, from the same fixtures test_boards parses."""
    linkedin = LinkedInGuest(delay=0)
    try:
        card = linkedin._card_to_job(LinkedInGuestCardTest.CARD)
    finally:
        linkedin.close()
    indeed = Indeed(domain='https://in.indeed.com', delay=0)
    try:
        listing = indeed._to_job(IndeedParseTest.ITEM)
    finally:
        indeed.close()
    naukri = Naukri()._api_job(
        {
            'jobId': 12345,
            'title': 'Python Developer',
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
    google = GoogleJobs()._to_job(
        'A\nSenior Python Developer\nAcme Corp\nBengaluru, India • via LinkedIn\n'
        '6 days ago\nFull-time\n₹20L–₹30L a year'
    )
    samples = {'linkedin': card, 'indeed': listing, 'naukri': naukri, 'googlejobs': google}
    assert all(samples.values())
    return {name: job for name, job in samples.items() if job is not None}


def carries(job: Job, field: Field) -> bool:
    """Whether a parsed job has a value for the field a capability names."""
    if field is Field.EXPERIENCE:
        return job.experience_min is not None or job.experience_max is not None
    if field is Field.POSTED:
        return bool(job.posted or job.posted_text)
    return getattr(job, field.value) is not None


# the fields whose silence the filters interpret, so a gap here changes results
FILTERED_ON = (Field.SALARY, Field.EXPERIENCE, Field.POSTED)


class CapabilityDeclarationTest(unittest.TestCase):
    """The declarations used to be comments citing line numbers; now they are checked."""

    def test_every_board_is_declared(self):
        self.assertEqual(set(CAPABILITIES), set(parsed_samples()))

    def test_every_field_a_board_claims_is_one_its_parser_fills(self):
        for name, job in parsed_samples().items():
            for field in CAPABILITIES[name].publishes:
                with self.subTest(board=name, field=field.value):
                    self.assertTrue(
                        carries(job, field), f'{name} claims {field} but never fills it'
                    )

    def test_no_board_fills_a_filtered_field_it_does_not_declare(self):
        """An undeclared field would be flagged -unpublished while the board says it."""
        for name, job in parsed_samples().items():
            for field in FILTERED_ON:
                if carries(job, field):
                    with self.subTest(board=name, field=field.value):
                        self.assertIn(field, CAPABILITIES[name].publishes)

    def test_fields_are_the_enum_and_still_compare_as_strings(self):
        declared = CAPABILITIES['linkedin'].filters
        self.assertTrue(all(isinstance(field, Field) for field in declared))
        self.assertIn('posted', declared)
        self.assertEqual(Field.POSTED, 'posted')


# -- the checks -------------------------------------------------------------


def context(**options) -> FilterContext:
    options.setdefault('today', date(2026, 8, 5))
    options.setdefault('rates', EMPTY)
    options.setdefault('policy', SilencePolicy())
    return FilterContext(**options)


def job(**fields) -> Job:
    fields.setdefault('source', 'naukri')
    fields.setdefault('title', 'Python Developer')
    return Job(**fields)


class CheckTest(unittest.TestCase):
    def test_a_filter_compiles_to_one_check_per_criterion_in_order(self):
        kinds = [
            type(check).__name__
            for check in JobFilter(
                title='python', location='India', min_salary=1, experience=3, posted_within_days=7
            ).checks()
        ]
        self.assertEqual(
            kinds, ['TextCheck', 'LocationCheck', 'SalaryCheck', 'ExperienceCheck', 'PostedCheck']
        )

    def test_an_empty_filter_has_no_checks(self):
        self.assertEqual(JobFilter(title=['', '  ']).checks(), [])

    def test_a_check_answers_with_a_verdict_and_a_reason(self):
        verdict = TextCheck(Field.TITLE, ('rust',))(job(), context())
        self.assertFalse(verdict.keep)
        self.assertEqual(verdict.reason, 'title did not match')

    def test_silence_is_the_boards_or_the_postings(self):
        board = {'naukri': Capability(publishes=frozenset({Field.SALARY}))}
        ctx = context(capability_of=lambda source: board.get(source or '', Capability()))
        check = SalaryCheck(1_000_000, 'INR', 'ppp')
        self.assertEqual(check(job(), ctx).flag, flags.SALARY_UNKNOWN)
        self.assertEqual(check(job(source='linkedin'), ctx).flag, flags.SALARY_UNPUBLISHED)

    def test_today_comes_from_the_context_not_the_clock(self):
        check = PostedCheck(7)
        posted = job(posted='2026-07-30')
        self.assertTrue(check(posted, context(today=date(2026, 8, 5))).keep)
        self.assertFalse(check(posted, context(today=date(2026, 9, 5))).keep)


class PurityTest(unittest.TestCase):
    def test_the_domain_filter_never_touches_the_network(self):
        """No rates handed in means no figures, reported - never a fetch."""
        with mock.patch.object(httpx.Client, 'send', side_effect=AssertionError('network!')):
            keep, found = JobFilter(min_salary=1_000_000, currency='INR').matches(
                job(salary='$120,000 a year')
            )
        self.assertTrue(keep)
        self.assertEqual(found, [flags.RATE_UNAVAILABLE])

    def test_a_snapshot_answers_what_the_live_table_would(self):
        live = Rates(path='/nonexistent/money-cache.json', offline=True)
        posting = job(salary='$120,000 a year')
        for basis in ('ppp', 'market'):
            with self.subTest(basis=basis):
                wired = WiredJobFilter(
                    min_salary=5_000_000, currency='INR', salary_basis=basis, rates=live
                )
                self.assertEqual(
                    wired.matches(posting), prepared(wired, [posting]).matches(posting)
                )


class RatesTest(unittest.TestCase):
    SNAPSHOT = RateSnapshot(
        market={'USD': 1.0, 'INR': 80.0, 'EUR': 0.5}, ppp_factors={'USD': 1.0, 'INR': 20.0}
    )

    def test_ppp_goes_through_the_international_dollar(self):
        self.assertEqual(convert(100, 'USD', 'INR', 'ppp', self.SNAPSHOT), (2000.0, None))

    def test_a_missing_factor_falls_back_to_market_and_says_so(self):
        self.assertEqual(
            convert(100, 'USD', 'EUR', 'ppp', self.SNAPSHOT), (50.0, flags.PPP_UNAVAILABLE)
        )

    def test_nothing_known_is_reported_not_guessed(self):
        self.assertEqual(
            convert(100, 'USD', 'JPY', 'market', EMPTY), (None, flags.RATE_UNAVAILABLE)
        )

    def test_market_rates_can_be_read_against_another_base(self):
        self.assertEqual(self.SNAPSHOT.fx('EUR')['INR'], 160.0)

    def test_a_snapshot_holds_only_the_currencies_asked_for(self):
        snap = Rates(path='/nonexistent/money-cache.json', offline=True).snapshot({'usd', 'inr'})
        self.assertEqual(set(snap.ppp_factors), {'USD', 'INR'})

    def test_market_rates_are_only_fetched_when_they_will_be_used(self):
        """PPP with every factor known never needed them, and never asked."""
        live = mock.Mock()
        live.ppp.side_effect = {'USD': 1.0, 'INR': 20.0, 'EUR': None}.get
        live.fx.return_value = {'USD': 1.0}
        Rates.snapshot(live, {'USD', 'INR'})
        live.fx.assert_not_called()
        Rates.snapshot(live, {'USD', 'EUR'})  # EUR has no factor: market is the fallback
        live.fx.assert_called_once()
        Rates.snapshot(live, {'USD', 'INR'}, basis='market')
        self.assertEqual(live.fx.call_count, 2)

    def test_one_currency_needs_no_figures(self):
        live = mock.Mock()
        self.assertEqual(Rates.snapshot(live, {'INR'}), RateSnapshot())
        live.fx.assert_not_called()


class PreparedTest(unittest.TestCase):
    def test_rates_are_fetched_once_for_the_whole_batch(self):
        table = mock.Mock()
        table.snapshot.return_value = RateSnapshot()
        wired = WiredJobFilter(min_salary=1, currency='INR', rates=table)
        batch = [job(salary='$1 a year'), job(salary='€2 a year'), job(salary='₹3 a year')]
        ready = prepared(wired, batch)
        table.snapshot.assert_called_once_with({'INR', 'USD', 'EUR'}, basis='ppp')
        self.assertIsInstance(ready.rates, RateSnapshot)

    def test_nothing_is_fetched_when_nothing_is_compared_across_currencies(self):
        table = mock.Mock()
        for wired in (
            WiredJobFilter(title='python', rates=table),
            WiredJobFilter(min_salary=1, currency='INR', salary_basis='strict', rates=table),
        ):
            with self.subTest(filter=wired):
                self.assertIs(prepared(wired, [job(salary='$1 a year')]), wired)
        table.snapshot.assert_not_called()


class FlagTest(unittest.TestCase):
    def test_the_stored_strings_do_not_change(self):
        """These are a file format: every applied_jobs.csv already written uses them."""
        self.assertEqual(
            [
                flags.SALARY_UNKNOWN,
                flags.EXPERIENCE_UNKNOWN,
                flags.DATE_UNKNOWN,
                flags.SALARY_UNPUBLISHED,
                flags.EXPERIENCE_UNPUBLISHED,
                flags.DATE_UNPUBLISHED,
                flags.LOCATION_UNVERIFIED,
                flags.SALARY_CURRENCY_MISMATCH,
                flags.SALARY_CURRENCY_ASSUMED,
                flags.PPP_UNAVAILABLE,
                flags.RATE_UNAVAILABLE,
                flags.CURRENCY_UNKNOWN,
                flags.EXPERIENCE_ENRICHED,
                flags.also_on('indeed'),
            ],
            [
                'salary-unknown',
                'experience-unknown',
                'date-unknown',
                'salary-unpublished',
                'experience-unpublished',
                'date-unpublished',
                'location-unverified',
                'salary-currency-mismatch',
                'salary-currency-assumed',
                'ppp-unavailable',
                'rate-unavailable',
                'currency-unknown',
                'experience-enriched',
                'also-on-indeed',
            ],
        )


class DedupeTest(unittest.TestCase):
    def test_the_copy_you_can_act_on_wins_and_remembers_the_others(self):
        google = job(source='googlejobs', company='Acme', location='Pune')
        indeed = job(source='indeed', id='1', company='Acme', location='Pune', url='https://i/1')
        (kept,) = dedupe.one_per_job([google, indeed])
        self.assertIs(kept, indeed)
        self.assertEqual(kept.flags, [flags.also_on('googlejobs')])

    def test_one_board_twice_is_two_jobs(self):
        first = job(source='indeed', id='1', company='Acme')
        second = job(source='indeed', id='2', company='Acme')
        self.assertEqual(len(dedupe.one_per_job([first, second])), 2)


if __name__ == '__main__':
    unittest.main()
