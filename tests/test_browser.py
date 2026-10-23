"""applicant.infra.browser: the one launcher, driven against a fake Playwright.

No browser is started here. `sync_playwright` is replaced by a mock whose
`chromium` refuses whichever channels a test says are not installed.
"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from applicant.boards.linkedin_apply import LinkedIn
from applicant.errors import SourceError
from applicant.infra.browser import STATE_FILE, BrowserSession, browser
from applicant.reviews import GlassdoorClient
from applicant.search import Jobs


class FakePlaywright:
    """Enough of Playwright for the launcher: channels, contexts and teardown."""

    def __init__(self, missing=()):
        self.driver = mock.Mock(name='playwright')
        self.missing = set(missing)
        self.launched: list[str | None] = []
        self.contexts: list[dict] = []  # the options each fresh context was made with
        chromium = self.driver.chromium
        chromium.launch.side_effect = self._launch
        chromium.launch_persistent_context.side_effect = self._launch_persistent

    def _check(self, channel):
        self.launched.append(channel)
        if channel in self.missing:
            raise RuntimeError(f'{channel or "bundled"} is not installed\nmore detail')

    def _launch(self, **options):
        self._check(options.get('channel'))
        instance = mock.Mock(name='browser')

        def new_context(**options):
            self.contexts.append(options)
            return self._context()

        instance.new_context.side_effect = new_context
        return instance

    def _launch_persistent(self, profile_dir, **options):
        self._check(options.get('channel'))
        return self._context()

    def _context(self):
        context = mock.Mock(name='context')
        context.pages = []

        def storage_state(path: str):
            Path(path).write_text('{"cookies": []}', encoding='utf-8')

        context.storage_state.side_effect = storage_state
        return context

    def patch(self):
        entry = mock.Mock()
        entry.return_value.start.return_value = self.driver
        return mock.patch('playwright.sync_api.sync_playwright', entry)


class TempDir(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)


class ChannelTest(TempDir):
    def test_an_installed_chrome_is_tried_first(self):
        fake = FakePlaywright()
        with fake.patch(), BrowserSession() as session:
            session.start()
        self.assertEqual(fake.launched, ['chrome'])

    def test_it_falls_back_through_edge_to_the_bundled_build(self):
        fake = FakePlaywright(missing={'chrome', 'msedge'})
        with fake.patch(), BrowserSession() as session:
            session.start()
        self.assertEqual(fake.launched, ['chrome', 'msedge', None])

    def test_nothing_launching_is_a_source_error_naming_each_try(self):
        fake = FakePlaywright(missing={'chrome', 'msedge', None})
        with fake.patch(), self.assertRaisesRegex(SourceError, 'chrome: chrome is not installed'):
            BrowserSession().start()
        fake.driver.stop.assert_called_once()  # the driver is not leaked

    def test_a_profile_makes_the_context_persistent(self):
        fake = FakePlaywright()
        profile = str(self.root / 'profile')
        with fake.patch(), BrowserSession(profile) as session:
            session.start()
        args, _ = fake.driver.chromium.launch_persistent_context.call_args
        self.assertEqual(args, (profile,))

    def test_a_saved_session_seeds_a_fresh_context(self):
        fake = FakePlaywright()
        state = {'cookies': [{'name': 'li_at'}], 'origins': []}
        with fake.patch(), BrowserSession(storage_state=state) as session:
            session.start()
        self.assertEqual([options['storage_state'] for options in fake.contexts], [state])


class TeardownTest(TempDir):
    def test_close_is_safe_twice(self):
        fake = FakePlaywright()
        with fake.patch():
            session = BrowserSession()
            session.start()
            session.close()
            session.close()
        fake.driver.stop.assert_called_once()

    def test_the_context_manager_form_closes(self):
        fake = FakePlaywright()
        with fake.patch(), browser():
            pass
        fake.driver.stop.assert_called_once()

    def test_a_session_never_started_closes_quietly(self):
        BrowserSession().close()


class StateTest(TempDir):
    def test_the_session_is_saved_inside_the_profile_owner_only(self):
        """It is a credential: not loose in the cwd, not world readable."""
        fake = FakePlaywright()
        profile = self.root / 'profile'
        with fake.patch(), BrowserSession(profile) as session:
            saved = session.save_state()
        self.assertEqual(saved, profile / STATE_FILE)
        self.assertEqual(stat.S_IMODE(os.stat(saved).st_mode), 0o600)

    def test_an_older_looser_file_is_tightened(self):
        fake = FakePlaywright()
        profile = self.root / 'profile'
        profile.mkdir()
        (profile / STATE_FILE).write_text('{}', encoding='utf-8')
        os.chmod(profile / STATE_FILE, 0o644)
        with fake.patch(), BrowserSession(profile) as session:
            saved = session.save_state()
        self.assertEqual(stat.S_IMODE(os.stat(saved).st_mode), 0o600)

    def test_glassdoor_saves_beside_its_profile(self):
        fake = FakePlaywright()
        profile = self.root / '.gd_profile'
        cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, cwd)
        with fake.patch(), BrowserSession(profile) as session:
            GlassdoorClient(profile_dir=str(profile))._save_state(session)
        self.assertTrue((profile / STATE_FILE).exists())
        self.assertFalse((self.root / STATE_FILE).exists(), 'nothing loose in the cwd')

    def test_a_linkedin_sign_in_is_saved_owner_only_making_its_directory(self):
        """cookies.json is a credential too, and --data-dir may not exist yet."""
        fake = FakePlaywright()
        target = self.root / 'new' / 'cookies.json'
        signed_in = mock.Mock(url='https://www.linkedin.com/feed/')
        with (
            fake.patch(),
            mock.patch.object(BrowserSession, 'page', new_callable=mock.PropertyMock) as page,
            LinkedIn(delay=0) as client,
        ):
            page.return_value = signed_in
            saved = client.login('someone', 'secret', filepath=target)
        self.assertEqual(saved, target)
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)

    def test_signing_in_again_tightens_an_older_session_file(self):
        fake = FakePlaywright()
        target = self.root / 'cookies.json'
        target.write_text('{}', encoding='utf-8')
        os.chmod(target, 0o644)
        signed_in = mock.Mock(url='https://www.linkedin.com/feed/')
        with (
            fake.patch(),
            mock.patch.object(BrowserSession, 'page', new_callable=mock.PropertyMock) as page,
            LinkedIn(delay=0) as client,
        ):
            page.return_value = signed_in
            client.login('someone', 'secret', filepath=target, overwrite=True)
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)


class LooksBlockedTest(unittest.TestCase):
    """The shared bot-check probe, on fake pages: the real interstitials change
    too often to pin, but the rules for reading one are ours."""

    def page(self, title='Senior Python Developer jobs', challenge=0):
        page = mock.MagicMock()
        page.title.return_value = title
        page.locator.return_value.count.return_value = challenge
        return page

    def test_a_challenge_title_is_a_bot_check(self):
        from applicant.infra.browser import looks_blocked

        for title in ('Just a moment...', 'Attention Required! | Cloudflare', 'Access Denied'):
            with self.subTest(title=title):
                self.assertTrue(looks_blocked(self.page(title=title)))

    def test_a_challenge_element_is_a_bot_check(self):
        from applicant.infra.browser import looks_blocked

        self.assertTrue(looks_blocked(self.page(challenge=1)))

    def test_an_ordinary_page_is_not(self):
        from applicant.infra.browser import looks_blocked

        self.assertFalse(looks_blocked(self.page()))

    def test_a_page_with_no_title_yet_is_not_judged(self):
        from applicant.infra.browser import looks_blocked

        page = self.page()
        page.title.side_effect = RuntimeError('navigating')
        self.assertFalse(looks_blocked(page))


class ClientForTest(unittest.TestCase):
    """Which board class each source name makes - without touching a network."""

    def test_each_source_is_its_own_board(self):
        from applicant.boards.googlejobs import GoogleJobs
        from applicant.boards.indeed import Indeed
        from applicant.boards.linkedin import LinkedInGuest
        from applicant.boards.naukri import Naukri

        with Jobs(headless=False) as board:
            for name, kind in (
                ('linkedin', LinkedInGuest),
                ('indeed', Indeed),
                ('naukri', Naukri),
                ('googlejobs', GoogleJobs),
            ):
                with self.subTest(source=name):
                    client = board._client(name)
                    self.addCleanup(client.close)
                    self.assertIsInstance(client, kind)
                    self.assertFalse(getattr(client, 'headless'))  # noqa: B009 - not on the Board protocol

    def test_a_search_reads_linkedin_as_a_guest(self):
        from applicant.boards.linkedin import LinkedInGuest

        client = Jobs()._client('linkedin')
        self.addCleanup(client.close)
        self.assertIs(type(client), LinkedInGuest)

    def test_an_unknown_source_is_a_source_error(self):
        with self.assertRaisesRegex(SourceError, 'unknown source'):
            Jobs()._client('monster')


class OwnershipTest(TempDir):
    """With no __del__, whoever opens a LinkedIn session closes it."""

    def test_linkedin_has_no_finaliser(self):
        self.assertNotIn('__del__', vars(LinkedIn))

    def test_linkedin_closes_its_browser(self):
        fake = FakePlaywright()
        with fake.patch(), LinkedIn(delay=0) as client:
            client._start()
        fake.driver.stop.assert_called_once()

    def test_the_facade_closes_the_linkedin_it_kept(self):
        linkedin = mock.Mock()
        with Jobs(linkedin=linkedin):
            pass
        linkedin.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
