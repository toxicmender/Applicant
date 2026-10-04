"""tools/record_page.py, on the scripted page: what it saves, and how it opens it."""

from __future__ import annotations

import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from tests.fakes import FakePage, FakeResponse, Visit, patched_browser

TOOL = Path(__file__).resolve().parent.parent / 'tools' / 'record_page.py'


def recorder():
    spec = importlib.util.spec_from_file_location('record_page', TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


URL = 'https://www.naukri.com/python-jobs'
HTML = '<html><p>Posted by ravi.k@example.org - Ravi Kumar, Hiring Lead</p></html>'


class RecordPageTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.tool = recorder()

    def record(self, page: FakePage, *extra: str):
        out = self.root / 'page.html'
        with (
            patched_browser(page) as session,
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            code = self.tool.main([URL, str(out), *extra])
        return code, out, session

    def page(self, *responses: FakeResponse) -> FakePage:
        return FakePage([(r'naukri', Visit(html=HTML, responses=list(responses)))])

    def test_the_page_is_saved_with_what_identifies_a_person_stripped(self):
        code, out, _ = self.record(self.page(), '--redact', r'Ravi Kumar')
        self.assertEqual(code, 0)
        saved = out.read_text(encoding='utf-8')
        self.assertNotIn('ravi.k@example.org', saved)
        self.assertNotIn('Ravi Kumar', saved)
        self.assertIn('Hiring Lead', saved)

    def test_the_api_response_is_saved_into_a_new_directory(self):
        api = FakeResponse(url='https://www.naukri.com/jobapi/v3/search', payload={'by': 'a@b.co'})
        target = self.root / 'new' / 'search.json'
        code, *_ = self.record(self.page(api), '--capture', '/jobapi/', str(target))
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(target.read_text(encoding='utf-8')), {'by': 'person@example.com'}
        )

    def test_no_matching_response_is_a_failure(self):
        code, *_ = self.record(self.page(), '--capture', '/jobapi/', str(self.root / 'x.json'))
        self.assertEqual(code, 1)

    def test_it_opens_the_page_as_the_sources_do(self):
        """Through applicant's launcher (Chrome before the bundled build), in
        the profile asked for - Naukri and Google refuse the bundled build."""
        _, _, session = self.record(self.page(), '--profile', '.gd_profile')
        self.assertEqual(session.options, [{'profile_dir': '.gd_profile', 'storage_state': None}])
        self.assertEqual(session.closed, 1)

    def test_a_page_that_never_goes_quiet_is_still_saved(self):
        page = self.page()
        page.wait_for_load_state = mock.Mock(side_effect=TimeoutError('networkidle never came'))
        code, out, _ = self.record(page)
        self.assertEqual(code, 0)
        self.assertTrue(out.exists())


if __name__ == '__main__':
    unittest.main()
