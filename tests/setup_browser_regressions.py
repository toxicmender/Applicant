"""Live setup regressions run in the real-Chromium CI tier."""

import json
import subprocess
import sys

from applicant.infra.browser import BrowserSession
from applicant.services.provider_setup import _verify_detailed, provider_session
from applicant.settings import Settings
from tests.fakes import RealBrowserTest
from tests.provider_setup_fixture_cli import COOKIE_VALUE, PAGE_TEXT, QUERY_VALUE


class SetupFailureBrowserTest(RealBrowserTest):
    def test_absent_health_db_preserves_successful_live_setup(self):
        for name in ('naukri', 'googlejobs', 'levels'):
            with self.subTest(provider=name):
                database = self.folder / f'{name}-health.db'
                result = self.cli(
                    name, 'signed-in', ['provider', name, '--database', str(database)], 0
                )
                assert result['status'] == 'verified', (
                    f'{name} setup: fresh-context restoration did not verify'
                )
                assert result['health_recovery']['status'] == 'skipped_missing_database', (
                    f'{name} recovery: expected a successful setup with the optional health database absent'
                )
                status = self.cli(name, 'no-worker', ['status', '--provider', name], 0)
                assert status['status'] == 'verified' and status['network_checked'] is False, (
                    f'{name} status: a separate offline process could not read the verified session'
                )
                assert not database.exists(), (
                    f'{name} recovery: setup created an unused health database'
                )

    def test_ambitionbox_reports_individual_control_visibility(self):
        profile = '<button data-testid="userProfile">Profile</button>'
        hidden = profile.replace('<button', '<button hidden')
        inbox = '<button data-testid="inboxBell">Inbox</button>'
        delayed = "<script>setTimeout(() => document.querySelector('button[hidden]').hidden = false, 350)</script>"
        for markup, expected in [
            (profile, {'profile': 'visible', 'inbox': 'absent'}),
            (hidden + inbox, {'profile': 'hidden', 'inbox': 'visible'}),
            ('<main></main>', {'profile': 'absent', 'inbox': 'absent'}),
            (hidden + profile + inbox, None),
            (hidden + inbox + delayed, None),
        ]:
            with self.subTest(expected=expected, delayed=delayed in markup):
                self.verify_markup(markup, expected)

    def verify_markup(self, markup, expected):
        class Session(BrowserSession):
            def start(self):
                fresh = self._page is None
                page = super().start()
                if fresh:
                    page.route(
                        '**/*', lambda route: route.fulfill(content_type='text/html', body=markup)
                    )
                return page

        result = _verify_detailed('ambitionbox', None, factory=Session)
        if expected is None:
            assert result['status'] == 'verified', (
                'ambitionbox verification: a visible match for each control must verify'
            )
        else:
            assert result.get('reason') == 'marker_missing', (
                'ambitionbox verification: missing controls must be inconclusive'
            )
            controls = result.get('controls')
            assert controls == expected, (
                f'ambitionbox verification: expected control states {expected}'
            )
            assert result.get('route_matched') is True, (
                'ambitionbox verification: diagnostics lost the matched route'
            )

    def cli(self, name, scenario, args, expected_code):
        config = self.folder / 'config.toml'
        config.write_text('session_storage = "plaintext"\nstore = "sqlite"\n', encoding='utf-8')
        log = self.folder / 'fixture.log'
        proc = subprocess.run(
            [
                sys.executable,
                '-m',
                'tests.provider_setup_fixture_cli',
                name,
                scenario,
                '--config',
                str(config),
                '--data-dir',
                str(self.folder),
                '--log-file',
                str(log),
                'setup',
                *args,
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        outputs = (proc.stdout, proc.stderr, log.read_text(encoding='utf-8'))
        leaked = any(
            secret in output
            for output in outputs
            for secret in (COOKIE_VALUE, PAGE_TEXT, QUERY_VALUE)
        )
        assert not leaked, f'{name} {scenario}: private fixture data appeared in a CLI diagnostic'
        code = proc.returncode
        assert code == expected_code, (
            f'{name} {scenario}: expected exit {expected_code}, got {code}'
        )
        return json.loads(proc.stdout)

    def test_ambitionbox_retry_command_uses_available_credentials(self):
        name = 'ambitionbox'
        store = provider_session(Settings(data_dir=self.folder, session_storage='plaintext'), name)
        first = self.cli(name, 'missing', ['provider', name], 5)
        assert first.get('reason') == 'marker_missing'
        assert first.get('stage') == 'capture', (
            'ambitionbox initial setup: expected capture-stage diagnostics'
        )
        assert first.get('controls') == {'profile': 'visible', 'inbox': 'absent'}
        assert first['command'] == 'applicant setup provider ambitionbox', (
            'ambitionbox retry: no credential was saved; a live status check cannot retry initial login'
        )
        args = ['status', '--provider', name, '--check', '--show']
        assert self.cli(name, 'no-worker', args, 3)['status'] == 'needs_setup'
        assert not store.path.exists(), (
            'ambitionbox status: checking an absent session created a credential'
        )
        assert self.cli(name, 'signed-in', ['provider', name], 0)['status'] == 'verified'
        before = store.path.read_bytes()
        result = self.cli(name, 'missing', args, 5)
        assert result.get('reason') == 'marker_missing'
        assert result.get('stage') == 'check'
        assert result['command'] == 'applicant setup status --provider ambitionbox --check --show'
        unchanged = store.path.read_bytes() == before
        assert unchanged, 'ambitionbox live check: missing controls changed a verified session'
