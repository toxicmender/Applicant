"""Check the installed Applicant launchers and parser contracts offline."""

import argparse
import io
import os
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import applicant
from applicant.cli import main

# argv, exit code, required output, forbidden output
CASES = [
    (['rates', '--help'], 0, '--refresh', '--profile'),
    (['apply', '--help'], 0, '--yes', '--saved'),
    (['rank', 'replay', '--help'], 0, '--provenance', '--top'),
    (['rank', 'compare', '--help'], 0, 'BASE_RUN', '--provenance'),
    (['rank', 'explain', '--help'], 0, 'JOB_ID', '--top'),
    (['rank', 'show', '--help'], 0, 'RUN_ID', '--provenance'),
    (['rank', 'list', '--help'], 0, '--profile', '--provenance'),
    (['rank', 'create', '--help'], 0, '--provenance', 'RUN_ID'),
    (['survey', 'export', '--help'], 0, 'PATH', '--json'),
    (['survey', 'show', '--help'], 0, '--revision', '--dry-run'),
    (['survey', 'import', '--help'], 0, '--dry-run', '--profile'),
    (['survey', 'validate', '--help'], 0, 'PATH', '--database'),
    (['survey', 'edit', '--help'], 0, '--profile', '--revision'),
    (['personalize', 'accept', '--help'], 0, '--suggestion', '--run'),
    (['personalize', 'suggest', '--help'], 0, '--run', '--suggestion'),
    (['feedback', 'import', '--help'], 0, '--from-file', '--decision'),
    (['feedback', 'record', '--help'], 0, '--expected-revision', '--history'),
    (['company-evidence', 'refresh', '--help'], 0, '--association', '--from-file'),
    (['company-evidence', 'show', '--help'], 0, '--snapshot', '--from-file'),
    (['company-evidence', 'enrich', '--help'], 0, '--export', '--dry-run'),
    (['company-evidence', 'import', '--help'], 0, '--dry-run', '--association'),
    (['company-evidence', 'associate', '--help'], 0, '--from-file', '--crunchbase-key'),
    (['benchmark', 'import', '--help'], 0, '--dry-run', '--query'),
    (['benchmark', 'discover', '--help'], 0, '--provider', '--snapshot'),
    (['benchmark', 'compare', '--help'], 0, '--selections', '--query'),
    (['benchmark', 'show', '--help'], 0, '--snapshot', '--query'),
    (['rates', '--currency', 'GBP', 'SEK'], 2, 'require --refresh', ''),
    (['financials', '--help'], 0, '--show', '--snapshot'),
    (['reviews', '--help'], 0, '--show', '--snapshot'),
    (['jobs', '--help'], 0, '--show', '--snapshot'),
    (['search', '--help'], 0, '--list-saved', '--database'),
    (['rank', '--list-runs'], 2, 'usage: applicant rank', ''),
    (['survey', '--from-file', 'unused'], 2, 'usage: applicant survey', ''),
    (['evaluate', '--help'], 0, '--feedback-through', '--decision'),
    (['db', 'upgrade', '--help'], 0, '--database', '--snapshot'),
    (['personalize', 'show', '--help'], 0, '--suggestion', '--run'),
    (['feedback', 'list', '--help'], 0, '--feedback-through', '--decision'),
    (['company-evidence', 'capture', '--help'], 0, '--association', '--from-file'),
    (['benchmark', 'fetch', '--query', 'unused', '--snapshot', 'bad'], 2, '--snapshot', ''),
    (['benchmark', 'fetch', '--help'], 0, '--query', '--snapshot'),
    ([], 2, 'usage: applicant', ''),
    (['--help'], 0, '--version', ''),
    (['status', '--help'], 0, '--json', ''),
    (['unknown-command'], 2, 'invalid choice', ''),
]


def check(require_installed: bool) -> None:
    started = time.perf_counter()
    source = Path(__file__).resolve().parents[1] / 'src'
    if require_installed and Path(applicant.__file__).resolve().is_relative_to(source):
        raise RuntimeError('expected an installed wheel, found the source tree')
    executable = Path(sys.executable).with_name('applicant.exe' if os.name == 'nt' else 'applicant')
    with tempfile.TemporaryDirectory() as folder:
        for launcher in ([str(executable)], [sys.executable, '-m', 'applicant']):
            result = subprocess.run(
                [*launcher, '--version'],
                cwd=folder,
                capture_output=True,
                text=True,
                timeout=45,
                check=True,
            )
            if result.stdout.strip() != f'applicant {applicant.__version__}' or result.stderr:
                raise RuntimeError(f'{launcher}: unexpected version output {result}')
        for argv, expected, required, forbidden in CASES:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                try:
                    code = main(['--no-log-file', '--data-dir', folder, *argv])
                except SystemExit as stop:
                    code = stop.code
            output, other = (err, out) if expected else (out, err)
            if (
                code != expected
                or required not in output.getvalue()
                or other.getvalue()
                or (forbidden and forbidden in output.getvalue())
            ):
                raise RuntimeError(
                    f'{argv}: exit {code}, stdout={out.getvalue()}, stderr={err.getvalue()}'
                )
    print(
        f'{sys.platform}: {len(CASES)} parser cases, two launchers, {time.perf_counter() - started:.2f}s'
    )


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--require-installed', action='store_true')
    check(parser.parse_args().require_installed)
