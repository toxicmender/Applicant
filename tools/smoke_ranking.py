"""Run a repeatable survey/ranking journey in separate CLI processes, offline.

uv run --frozen --isolated --no-editable python tools/smoke_ranking.py --require-installed
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import applicant

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / 'tests' / 'fixtures' / 'ranking'


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def journey(*, require_installed: bool = False) -> dict:
    if require_installed:
        expect(
            not Path(applicant.__file__).resolve().is_relative_to(ROOT / 'src'),
            'smoke test imported the editable source tree rather than an installed wheel',
        )
    expected = json.loads((FIXTURES / 'cli-journey.json').read_text(encoding='utf-8'))
    environment = dict(os.environ)
    for key in ('APPLICANT_HOME', 'APPLICANT_CONFIG', 'APPLICANT_STORE', 'APPLICANT_MONEY_CACHE'):
        environment.pop(key, None)
    environment['PYTHONIOENCODING'] = 'utf-8'
    with tempfile.TemporaryDirectory(prefix='applicant-cli-') as temporary:
        data = Path(temporary) / 'journey # 测试'
        data.mkdir()

        def cli(*args: str, code: int = 0) -> str:
            result = subprocess.run(
                [
                    sys.executable,
                    '-m',
                    'applicant',
                    '--no-log-file',
                    '--data-dir',
                    str(data),
                    *args,
                ],
                cwd=data,
                env=environment,
                capture_output=True,
                encoding='utf-8',
                timeout=45,
            )
            expect(
                result.returncode == code,
                f'{args}: exit {result.returncode}, expected {code}\n{result.stderr}\n{result.stdout}',
            )
            return result.stdout

        prefs = str(FIXTURES / 'preferences.json')
        provenance = str(FIXTURES / 'provenance.json')
        versions = str(FIXTURES / 'cli-versions.json')
        cli('survey', '--validate', prefs)
        cli('survey', '--dry-run', '--from-file', prefs)
        expect(not (data / 'applicant.db').exists(), 'validation/dry-run created a database')
        cli('survey', '--from-file', prefs)
        first = json.loads(
            cli(
                'rank',
                '--provenance',
                provenance,
                '--job-versions',
                versions,
                '--offline',
                '--json',
            )
        )
        run_id = first['run']['ranking_run_id']
        counts = {state: group['total'] for state, group in first['groups'].items()}
        expect(
            counts == expected['group_counts'], 'mixed eligibility groups differ from golden data'
        )
        expect(
            first['groups']['eligible']['results'][0]['overall_score']
            == expected['eligible_score'],
            'eligible score differs from golden data',
        )
        review = first['groups']['review_required']['results'][0]
        explanation = json.loads(
            cli('rank', '--run', run_id, '--explain', review['job_id'], '--json')
        )
        criteria = explanation['result']['decision']['criteria']
        conflict = next(c for c in criteria if c['reason_code'] == expected['review_reason'])[
            'conflicts'
        ][0]
        expect(
            len(conflict['assertions']) == 2 and bool(conflict['needs_verification']),
            'explanation lost conflicting assertions or verification prompt',
        )
        excluded = first['groups']['excluded']['results'][0]
        expect(
            any(
                c['reason_code'] == expected['excluded_reason']
                for c in excluded['decision']['criteria']
            ),
            'excluded reason differs from golden data',
        )

        cli('survey', '--export', 'edit.json')
        edited = json.loads((data / 'edit.json').read_text(encoding='utf-8'))
        edited['preferences']['preferred_base_salary'] = '120000'
        (data / 'edit.json').write_text(json.dumps(edited), encoding='utf-8')
        cli('survey', '--from-file', 'edit.json')
        # Inputs that would break/change creation must have no effect on history.
        (data / 'job_listing.json').write_text('{broken', encoding='utf-8')
        config = explanation['configuration'] | {
            'annual_hours': '2000',
            'scoring_config_id': 'smoke-later-config',
        }
        (data / 'config.json').write_text(json.dumps(config), encoding='utf-8')
        cli(
            'rank',
            '--provenance',
            provenance,
            '--job-versions',
            versions,
            '--scoring-config',
            'config.json',
            '--offline',
            '--json',
        )
        database = data / 'applicant.db'
        original = database.read_bytes()
        repeated = json.loads(cli('rank', '--run', run_id, '--json'))
        expect(repeated == first, 'original report changed after current inputs changed')
        listing = json.loads(cli('rank', '--list-runs', '--json'))
        expect(len(listing['runs']) == 2, 'run listing omitted a completed run')
        current = json.loads(cli('survey', '--show', '--json'))
        replay = json.loads(cli('rank', '--replay', run_id, '--provenance', provenance, '--json'))
        cli('rank', '--run', run_id, '--explain', 'not-a-member', code=4)
        expect(database.read_bytes() == original, 'historical commands changed database content')
        summary = {
            'group_counts': counts,
            'original_revision': repeated['run']['profile_revision'],
            'current_revision': current['profile_revision'],
            'replay_matches': replay['verification']['matches'],
        }
        expect(
            all(summary[key] == expected[key] for key in summary),
            'journey summary differs from golden data',
        )
        return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--require-installed', action='store_true')
    args = parser.parse_args(argv)
    print(json.dumps(journey(require_installed=args.require_installed), sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
