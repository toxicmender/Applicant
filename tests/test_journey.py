"""A user's whole run through the CLI, in one data directory, on both stores.

Each command is tested on its own elsewhere; this is the seams between them -
the paths `file_arg` resolves under `--data-dir`, the store each command opens,
the listing one command writes and the next reads, the log `apply` keeps and
`status` counts:

    search -> search again (merged) -> apply --dry-run -> apply --yes
           -> status -> apply --yes again (nothing left to send)
"""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from applicant.cli import main
from applicant.domain.job import Job
from applicant.search import Jobs
from applicant.storage import ApplicationLog, load_jobs
from tests.fakes import FakeBoard


def posting(source: str, n: int) -> Job:
    return Job(
        source=source,
        id=f'{source[0]}{n}',
        title=f'Python Developer {n}',
        company=f'Company {source}{n}',
        url=f'https://{source}.example/jobs/{n}',
    )


class Applier:
    """A signed-in LinkedIn that confirms every application it is asked for."""

    def __init__(self):
        self.asked: list[list[str]] = []
        self.applied: list[str] = []
        self.unconfirmed: list[str] = []
        self.failed: dict[str, str] = {}

    def easy_apply(self, jobs):
        self.applied = [job.url for job in jobs]
        self.asked.append(list(self.applied))
        return self.applied

    def close(self):
        pass


class JourneyTest(unittest.TestCase):
    store = 'files'

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.applier = Applier()
        self.boards: dict[str, FakeBoard] = {}
        for patch in (
            mock.patch.object(Jobs, '_client', lambda _, name: self.boards[name]),
            mock.patch.object(Jobs, 'linkedin', lambda _: self.applier),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = main(
                [*argv, '--data-dir', str(self.root), '--store', self.store, '--no-log-file']
            )
        return code, out.getvalue()

    def search(self, linkedin: list[Job], indeed: list[Job]) -> int:
        self.boards = {'linkedin': FakeBoard(linkedin), 'indeed': FakeBoard(indeed)}
        return self.cli('search', 'python', '--source', 'linkedin', 'indeed')[0]

    def logged(self) -> dict[str, list[str]]:
        rows: dict[str, list[str]] = {}
        for row in ApplicationLog(str(self.root / 'applied_jobs.csv')).rows():
            rows.setdefault(row['id'], []).append(row['status'])
        return rows

    def test_search_apply_and_status_share_one_data_directory(self):
        # two searches: the second's listing is merged into the first's
        self.assertEqual(
            self.search([posting('linkedin', 1), posting('linkedin', 2)], [posting('indeed', 1)]), 0
        )
        self.assertEqual(self.search([posting('linkedin', 2), posting('linkedin', 3)], []), 0)
        listing = load_jobs(str(self.root / 'job_listing.json'))
        self.assertEqual(
            sorted(job.id for job in listing), ['i1', 'l1', 'l2', 'l3'], 'merged, no duplicates'
        )

        # a rehearsal: logged, and nothing sent
        self.assertEqual(self.cli('apply', '--dry-run')[0], 0)
        self.assertEqual(self.applier.asked, [])
        self.assertEqual(
            self.logged(),
            {
                'l1': ['would_apply'],
                'l2': ['would_apply'],
                'l3': ['would_apply'],
                'i1': ['needs_manual_apply'],
            },
        )

        # the real thing: the rehearsal does not stop it, the Indeed row is settled
        self.assertEqual(self.cli('apply', '--yes')[0], 0)
        self.assertEqual(len(self.applier.asked), 1)
        self.assertEqual(
            sorted(self.applier.asked[0]), [f'https://linkedin.example/jobs/{n}' for n in (1, 2, 3)]
        )
        for job_id in ('l1', 'l2', 'l3'):
            self.assertEqual(self.logged()[job_id], ['would_apply', 'applied'])
        self.assertEqual(self.logged()['i1'], ['needs_manual_apply'])

        # status reads what search and apply wrote
        code, out = self.cli('status')
        self.assertEqual(code, 0)
        self.assertIn('4 jobs stored', out)
        self.assertIn('7 applications recorded', out)
        self.assertIn('applied: 3', out)

        # and a third run has nothing left to send
        self.assertEqual(self.cli('apply', '--yes')[0], 0)
        self.assertEqual(len(self.applier.asked), 1, 'nothing settled is sent again')
        self.assertEqual(sum(len(statuses) for statuses in self.logged().values()), 7)


class SqliteJourneyTest(JourneyTest):
    """The same journey with applicant.db as the record and the files as exports."""

    store = 'sqlite'

    def test_the_database_holds_what_the_files_show(self):
        self.test_search_apply_and_status_share_one_data_directory()
        self.assertTrue((self.root / 'applicant.db').exists())


if __name__ == '__main__':
    unittest.main()
