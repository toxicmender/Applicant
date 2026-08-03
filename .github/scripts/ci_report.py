#!/usr/bin/env python3
"""Turn CI tool output into a markdown summary and a durable status record.

Kept as a script rather than inline YAML so it can be run - and read - locally:

    python .github/scripts/ci_report.py pyright pyright.json
    python .github/scripts/ci_report.py junit 'junit-*.xml'
    python .github/scripts/ci_report.py status --jobs jobs.json --out status.json

Standard library only: it runs before, and independently of, `uv sync`.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import xml.etree.ElementTree as ET

# how a job's `needs.<job>.result` reads in the summary. A skipped or cancelled
# job is deliberately not folded into "pass" - that would hide a broken run.
RESULT_ICONS = {
    'success': '✅',
    'failure': '❌',
    'skipped': '⏭️',
    'cancelled': '🚫',
}

MAX_LISTED = 50


def read_junit(pattern: str) -> list[dict]:
    """Per-suite totals from JUnit XML, which both pytest and unittest can emit."""
    suites = []
    for path in sorted(glob.glob(pattern)):
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            continue
        # pytest writes <testsuites><testsuite>, some writers only the inner one
        node = root if root.tag == 'testsuite' else next(iter(root), None)
        if node is None:
            continue
        suites.append(
            {
                'file': os.path.basename(path),
                'tests': int(node.get('tests', 0)),
                'failures': int(node.get('failures', 0)),
                'errors': int(node.get('errors', 0)),
                'skipped': int(node.get('skipped', 0)),
                'time': float(node.get('time', 0)),
            }
        )
    return suites


def junit_table(suites: list[dict]) -> list[str]:
    if not suites:
        return ['No test reports were produced.']
    lines = ['| suite | tests | failed | skipped | time |', '|---|---|---|---|---|']
    for suite in suites:
        lines.append(
            '| `{}` | {} | {} | {} | {:.1f}s |'.format(
                suite['file'],
                suite['tests'],
                suite['failures'] + suite['errors'],
                suite['skipped'],
                suite['time'],
            )
        )
    return lines


def pyright_summary(path: str) -> tuple[int, list[str]]:
    """-> (error count, markdown lines)."""
    try:
        with open(path, encoding='utf-8') as handle:
            report = json.load(handle)
    except (OSError, ValueError) as error:
        return -1, ['Could not read `{}`: {}'.format(path, error)]

    errors = [d for d in report.get('generalDiagnostics', []) if d.get('severity') == 'error']
    if not errors:
        return 0, ['### ✅ pyright: no type errors']

    lines = ['### ⚠️ pyright: {} error(s)'.format(len(errors)), '', '```']
    for diagnostic in errors[:MAX_LISTED]:
        where = '{}:{}'.format(
            os.path.basename(diagnostic.get('file', '?')),
            diagnostic.get('range', {}).get('start', {}).get('line', -1) + 1,
        )
        lines.append('{} - {}'.format(where, diagnostic.get('message', '').splitlines()[0]))
    if len(errors) > MAX_LISTED:
        lines.append('... and {} more'.format(len(errors) - MAX_LISTED))
    lines.append('```')
    return len(errors), lines


def build_status(jobs: dict, suites: list[dict]) -> dict:
    return {
        'run_id': os.environ.get('GITHUB_RUN_ID', ''),
        'run_number': int(os.environ.get('GITHUB_RUN_NUMBER', 0) or 0),
        'sha': os.environ.get('GITHUB_SHA', ''),
        'ref': os.environ.get('GITHUB_REF_NAME', ''),
        'event': os.environ.get('GITHUB_EVENT_NAME', ''),
        'jobs': jobs,
        'suites': suites,
        'totals': {
            'tests': sum(s['tests'] for s in suites),
            'failed': sum(s['failures'] + s['errors'] for s in suites),
            'skipped': sum(s['skipped'] for s in suites),
        },
    }


def status_table(jobs: dict) -> list[str]:
    lines = ['| job | ran | tool result | detail |', '|---|---|---|---|']
    for name, job in jobs.items():
        result = job.get('result', 'unknown')
        lines.append(
            '| `{}` | {} {} | {} | {} |'.format(
                name,
                RESULT_ICONS.get(result, '❔'),
                result,
                job.get('outcome') or 'n/a',
                job.get('detail', ''),
            )
        )
    return lines


def emit(lines: list[str]) -> None:
    """Print, and append to the GitHub step summary when there is one."""
    text = '\n'.join(lines)
    print(text)
    target = os.environ.get('GITHUB_STEP_SUMMARY')
    if target:
        with open(target, 'a', encoding='utf-8') as handle:
            handle.write(text + '\n')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)

    pyright = commands.add_parser('pyright', help='summarise a pyright --outputjson report')
    pyright.add_argument('path')

    junit = commands.add_parser('junit', help='summarise JUnit XML reports')
    junit.add_argument('pattern')

    status = commands.add_parser('status', help='collect everything into status.json')
    status.add_argument(
        '--jobs', required=True, help='JSON file of {job: {result, outcome, detail}}'
    )
    status.add_argument('--junit', default='reports/junit-*.xml')
    status.add_argument('--out', default='status.json')

    args = parser.parse_args(argv)

    if args.command == 'pyright':
        count, lines = pyright_summary(args.path)
        emit(lines)
        # the report is the product; a type error must not fail this job
        return 0 if count >= 0 else 1

    if args.command == 'junit':
        emit(junit_table(read_junit(args.pattern)))
        return 0

    with open(args.jobs, encoding='utf-8') as handle:
        jobs = json.load(handle)

    suites = read_junit(args.junit)
    status_record = build_status(jobs, suites)
    with open(args.out, 'w', encoding='utf-8') as handle:
        json.dump(status_record, handle, indent=2)

    totals = status_record['totals']
    emit(
        [
            '## CI status',
            '',
            *status_table(jobs),
            '',
            *junit_table(suites),
            '',
            '{} tests, {} failed, {} skipped.'.format(
                totals['tests'], totals['failed'], totals['skipped']
            ),
            '',
            'Nothing here gates a merge - this is a report. '
            'Formatting is fixed automatically by the `format` workflow.',
        ]
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())
