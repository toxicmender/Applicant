import json
import subprocess
import sys

import pytest

from applicant.domain.providers import provider
from applicant.services.provider_setup import provider_session
from applicant.settings import Settings


@pytest.mark.parametrize(
    'name', ['indeed', 'naukri', 'googlejobs', 'glassdoor', 'ambitionbox', 'levels']
)
def test_session_import_status_and_clear_in_fresh_processes(tmp_path, name):
    config = tmp_path / 'applicant.toml'
    config.write_text('store = "files"\nsession_storage = "plaintext"\n', encoding='utf-8')
    source = tmp_path / 'external.json'
    policy = provider(name).session
    assert policy is not None
    state = {
        'cookies': [
            {
                'name': 'account',
                'value': 'synthetic',
                'domain': policy.storage_domains[0],
                'path': '/',
            },
        ],
        'origins': [],
    }
    original = json.dumps(state).encode()
    source.write_bytes(original)

    def cli(*args, expected=0):
        result = subprocess.run(
            [
                sys.executable,
                '-m',
                'applicant',
                '--no-log-file',
                '--config',
                str(config),
                '--data-dir',
                str(tmp_path),
                'setup',
                *args,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == expected, result.stderr
        return json.loads(result.stdout)

    assert (
        cli('import-session', '--provider', name, '--from-file', str(source))['status']
        == 'unverified'
    )
    assert cli('status', '--provider', name)['status'] == 'unverified'
    assert cli('clear', '--provider', name)['status'] == 'cleared'
    assert cli('status', '--provider', name, expected=3)['status'] == 'needs_setup'
    store = provider_session(Settings(data_dir=tmp_path), name)
    assert store.snapshot()['status'] == 'cleared'
    assert source.read_bytes() == original
