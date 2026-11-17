"""Check every supported type target, retaining all reports even after a failure."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

TARGETS = tuple(
    (platform, version) for platform in ('Linux', 'Windows') for version in ('3.10', '3.12')
)


def read_report(path: Path) -> tuple[int, list[str]]:
    """Reject incomplete or inconsistent reports rather than assuming zero errors."""
    report = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(report, dict) or not isinstance(report.get('summary'), dict):
        raise ValueError('missing summary')
    count = report['summary'].get('errorCount')
    diagnostics = report.get('generalDiagnostics')
    if type(count) is not int or count < 0 or not isinstance(diagnostics, list):
        raise ValueError('invalid error count or diagnostics')
    messages = []
    for diagnostic in diagnostics:
        if not isinstance(diagnostic, dict) or diagnostic.get('severity') not in (
            'error',
            'warning',
            'information',
        ):
            raise ValueError('invalid diagnostic')
        if diagnostic['severity'] == 'error':
            messages.append(str(diagnostic.get('message', 'Unspecified type error')))
    if count != len(messages):
        raise ValueError('error count disagrees with diagnostics')
    return count, messages


def append_environment_file(variable: str, text: str) -> None:
    if target := os.environ.get(variable):
        with Path(target).open('a', encoding='utf-8') as handle:
            handle.write(text + '\n')


def main() -> int:
    failed = False
    details = []
    for platform, version in TARGETS:
        label = f'{platform} / Python {version}'
        report = Path(f'pyright-{platform}-{version}.json')
        stderr = report.with_suffix('.stderr.txt')
        lines = [f'### {label}']
        try:
            # Opening in write mode prevents an earlier successful run from masking a crash.
            with (
                report.open('w', encoding='utf-8') as output,
                stderr.open('w', encoding='utf-8') as errors,
            ):
                result = subprocess.run(
                    [
                        sys.executable,
                        '-m',
                        'pyright',
                        '--pythonplatform',
                        platform,
                        '--pythonversion',
                        version,
                        '--outputjson',
                    ],
                    stdout=output,
                    stderr=errors,
                    check=False,
                )
            try:
                count, messages = read_report(report)
                detail = f'{count} error(s), exit {result.returncode}'
                failed |= result.returncode != 0 or count != 0
                lines.extend(messages[:50])
            except (OSError, ValueError) as error:
                failed = True
                detail = f'invalid report, exit {result.returncode}: {error}'
        except OSError as error:
            failed = True
            detail = f'could not run type check: {error}'
        details.append(f'{label}: {detail}')
        lines.insert(1, detail)
        lines.append(f'Report: `{report}`. Tool stderr: `{stderr}`.')
        text = '\n\n'.join(lines)
        print(text, flush=True)
        append_environment_file('GITHUB_STEP_SUMMARY', text)
    outcome = 'fail' if failed else 'pass'
    append_environment_file('GITHUB_OUTPUT', f'outcome={outcome}')
    # GitHub single-line outputs must not inherit newlines from exception messages.
    append_environment_file('GITHUB_OUTPUT', 'detail=' + ' '.join('; '.join(details).splitlines()))
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
