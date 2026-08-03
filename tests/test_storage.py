from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from applicant.models import Job
from applicant.storage import APPLIED_COLUMNS, ApplicationLog, load_jobs, save_jobs


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)

    def path(self, name: str) -> str:
        return str(self.root / name)


class SaveJobsTest(TempDirTest):
    def test_writes_a_list_wrapper(self):
        target = self.path('jobs.json')
        total = save_jobs([Job(source='indeed', id='1', title='Dev')], target)

        self.assertEqual(total, 1)
        with open(target, encoding='utf-8') as handle:
            self.assertEqual(len(json.load(handle)['list']), 1)

    def test_merges_across_runs(self):
        target = self.path('jobs.json')
        save_jobs([Job(source='indeed', id='1', title='Dev')], target)
        total = save_jobs([Job(source='indeed', id='2', title='Other')], target)

        self.assertEqual(total, 2)

    def test_the_newest_write_wins(self):
        target = self.path('jobs.json')
        save_jobs([Job(source='indeed', id='1', title='Old title')], target)
        save_jobs([Job(source='indeed', id='1', title='New title')], target)

        jobs = load_jobs(target)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].title, 'New title')

    def test_the_same_id_on_two_boards_is_two_jobs(self):
        target = self.path('jobs.json')
        total = save_jobs(
            [Job(source='indeed', id='1', title='A'), Job(source='naukri', id='1', title='B')],
            target,
        )
        self.assertEqual(total, 2)

    def test_idless_postings_do_not_collapse_into_one(self):
        """Google Jobs exposes no id; keying on source alone would lose every row."""
        target = self.path('jobs.json')
        total = save_jobs(
            [
                Job(source='googlejobs', id=None, title='A', company='Acme', location='Pune'),
                Job(source='googlejobs', id=None, title='B', company='Acme', location='Pune'),
            ],
            target,
        )
        self.assertEqual(total, 2)

    def test_a_corrupt_file_is_overwritten_not_fatal(self):
        target = self.path('jobs.json')
        Path(target).write_text('{not json', encoding='utf-8')

        total = save_jobs([Job(source='indeed', id='1', title='Dev')], target)
        self.assertEqual(total, 1)

    def test_unicode_survives_the_round_trip(self):
        target = self.path('jobs.json')
        save_jobs([Job(source='naukri', id='1', title='Dev', salary='₹25K a month')], target)
        self.assertEqual(load_jobs(target)[0].salary, '₹25K a month')


class LoadJobsTest(TempDirTest):
    def test_a_missing_file_is_empty(self):
        self.assertEqual(load_jobs(self.path('nope.json')), [])

    def test_a_corrupt_file_is_empty(self):
        target = self.path('jobs.json')
        Path(target).write_text('nonsense', encoding='utf-8')
        self.assertEqual(load_jobs(target), [])

    def test_unknown_stored_fields_are_ignored(self):
        target = self.path('jobs.json')
        Path(target).write_text(
            json.dumps(
                {
                    'list': [
                        {'source': 'indeed', 'title': 'Dev', 'id': '1', 'retired_field': 'x'},
                    ]
                }
            ),
            encoding='utf-8',
        )

        jobs = load_jobs(target)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].title, 'Dev')


class ApplicationLogTest(TempDirTest):
    def entry(self, job_id='1', status='applied', note='ok', **kwargs):
        fields: dict[str, Any] = {
            'source': 'linkedin',
            'id': job_id,
            'title': 'Dev',
            'company': 'Acme',
        }
        fields.update(kwargs)
        return (Job(**fields), status, note)

    def test_writes_a_header_once(self):
        target = self.path('applied.csv')
        log = ApplicationLog(target)
        log.record([self.entry('1')])
        log.record([self.entry('2')])

        with open(target, encoding='utf-8-sig', newline='') as handle:
            lines = [line for line in handle.read().splitlines() if line.strip()]
        self.assertEqual(len(lines), 3, 'header plus two rows')
        self.assertEqual(next(csv.reader([lines[0]])), APPLIED_COLUMNS)

    def test_the_same_posting_is_never_recorded_twice(self):
        target = self.path('applied.csv')
        log = ApplicationLog(target)

        self.assertEqual(log.record([self.entry('1')]), 1)
        self.assertEqual(log.record([self.entry('1')]), 0)
        self.assertEqual(len(log.rows()), 1)

    def test_duplicates_within_one_call_are_collapsed(self):
        target = self.path('applied.csv')
        written = ApplicationLog(target).record([self.entry('1'), self.entry('1')])
        self.assertEqual(written, 1)

    def test_written_with_a_bom_so_sheets_reads_currency_symbols(self):
        target = self.path('applied.csv')
        ApplicationLog(target).record([self.entry('1', salary='₹25K a month')])

        self.assertTrue(Path(target).read_bytes().startswith(b'\xef\xbb\xbf'))
        self.assertIn('₹25K a month', Path(target).read_text(encoding='utf-8-sig'))

    def test_salary_is_expanded_into_annual_columns(self):
        target = self.path('applied.csv')
        ApplicationLog(target).record([self.entry('1', salary='2-2.5 Lacs PA')])

        row = ApplicationLog(target).rows()[0]
        self.assertEqual(row['salary_annual_low'], '200000.0')
        self.assertEqual(row['salary_annual_high'], '250000.0')
        self.assertEqual(row['currency'], 'INR')

    def test_an_unknown_salary_leaves_the_derived_columns_empty(self):
        target = self.path('applied.csv')
        ApplicationLog(target).record([self.entry('1', salary='Not disclosed')])

        row = ApplicationLog(target).rows()[0]
        self.assertEqual(row['salary_annual_low'], '')
        self.assertEqual(row['currency'], '')

    def test_flags_are_written_space_separated(self):
        target = self.path('applied.csv')
        ApplicationLog(target).record([self.entry('1', flags=['salary-unknown', 'date-unknown'])])

        self.assertEqual(ApplicationLog(target).rows()[0]['flags'], 'salary-unknown date-unknown')

    def test_nothing_to_record_writes_no_file(self):
        target = self.path('applied.csv')
        self.assertEqual(ApplicationLog(target).record([]), 0)
        self.assertFalse(Path(target).exists())

    def test_counts_tallies_by_status(self):
        target = self.path('applied.csv')
        ApplicationLog(target).record(
            [
                self.entry('1', status='applied'),
                self.entry('2', status='needs_manual_apply'),
                self.entry('3', status='needs_manual_apply'),
            ]
        )

        self.assertEqual(ApplicationLog(target).counts(), {'applied': 1, 'needs_manual_apply': 2})

    def test_counts_on_a_missing_log_is_empty(self):
        self.assertEqual(ApplicationLog(self.path('nope.csv')).counts(), {})


if __name__ == '__main__':
    unittest.main()
