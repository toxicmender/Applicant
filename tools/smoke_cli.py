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
