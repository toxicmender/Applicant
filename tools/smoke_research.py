"""Separate CLI processes verify the installed research journey without network."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--require-installed', action='store_true')
    args = parser.parse_args()
    import applicant

    root = Path(__file__).resolve().parents[1]
    if args.require_installed and root / 'src' in Path(applicant.__file__).resolve().parents:
        raise RuntimeError('requires an isolated non-editable installed package')
    fixture = root / 'tests' / 'fixtures' / 'research' / 'journey.json'
    with tempfile.TemporaryDirectory() as folder:
        data = Path(folder)
        bootstrap = data / 'bootstrap'
        bootstrap.mkdir()
        # No source package is exposed through PYTHONPATH in the installed journey.
        (bootstrap / 'sitecustomize.py').write_text(
            'import socket\n'
            'def blocked(*a, **k): raise RuntimeError("network disabled in research smoke")\n'
            'socket.create_connection = blocked\n'
            'import applicant.domain.contracts as contracts\n'
            'from datetime import datetime, timezone\n'
            'contracts.now = lambda: datetime(2026,11,10,12,tzinfo=timezone.utc)\n',
            encoding='utf-8',
        )
        environment = dict(
            os.environ, PYTHONPATH=str(bootstrap), PYTHONUTF8='1', PYTHONIOENCODING='utf-8'
        )

        def run(*arguments):
            completed = subprocess.run(
                [
                    sys.executable,
                    '-m',
                    'applicant',
                    '--data-dir',
                    folder,
                    '--no-log-file',
                    *arguments,
                ],
                cwd=data,
                env=environment,
                capture_output=True,
                text=True,
                encoding='utf-8',
                timeout=60,
            )
            if completed.returncode:
                raise RuntimeError(completed.stderr or completed.stdout)
            return json.loads(completed.stdout)

        run('research', 'import', '--from-file', str(fixture))
        feed = run('feed', 'collect', '--from-imports')
        assert any(group['status'] == 'confirmed' for group in feed['groups'])
        snapshot = run(
            'analytics', 'technologies', '--as-of', '2026-11-10', '--timezone', 'Asia/Kolkata'
        )
        before = run('analytics', 'show', '--snapshot', snapshot['snapshot_id'])
        assert before['rows'][0]['observed_openings'] == 6
        selection = data / 'selection.json'
        selection.write_text(
            '{"entity_ids":["startup"],"timezone":"Asia/Kolkata"}', encoding='utf-8'
        )
        run(
            'research',
            'report',
            '--selection',
            str(selection),
            '--summary',
            '--output',
            'report.json',
            '--pdf',
            'report.pdf',
        )
        run(
            'report',
            'render',
            '--from-file',
            'report.json',
            '--format',
            'markdown',
            '--output',
            'report.md',
        )
        run(
            'report',
            'render',
            '--from-file',
            'report.md',
            '--pdf',
            'rerendered.pdf',
            '--output',
            'rerendered.json',
        )
        assert json.loads((data / 'report.json').read_text(encoding='utf-8')) == json.loads(
            (data / 'rerendered.json').read_text(encoding='utf-8')
        )
        assert before == run('analytics', 'show', '--snapshot', snapshot['snapshot_id'])
        run('store', 'checkpoint', '--mode', 'truncate')
    print(
        'Research smoke passed: cross-board fixture, frozen analytics, JSON/Markdown/PDF, checkpoint.'
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
