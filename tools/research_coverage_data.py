"""Record and combine compatible coverage artifacts from the native CI jobs."""

import argparse
import hashlib
import inspect
import json
import platform
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

from coverage import CoverageData, __version__

from applicant.infra.owned_worker import _child
from applicant.infra.path_identity import physical_path


def digest(path):
    return hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest()


def provenance():
    return {
        'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
        'python_minor': list(platform.python_version_tuple()[:2]),
        'coverage': __version__,
        'config_sha256': digest(Path('pyproject.toml')),
        'source_sha256': {
            p.as_posix(): digest(p) for p in sorted(Path('src/applicant').rglob('*.py'))
        },
        'branch_coverage': True,
        'origin': 'checkout',
    }


def canonical_path(filename, sources, *, checkout=None):
    if checkout is None:
        name = filename
    else:
        repo = physical_path(checkout)
        recorded = physical_path(repo / filename)
        if not recorded.is_relative_to(repo) or not any(
            parent.samefile(repo) for parent in recorded.parents if parent.exists()
        ):
            raise ValueError(f'unmapped coverage path: {filename}')
        name = recorded.relative_to(repo).as_posix()
    if name not in sources:
        raise ValueError(f'unmapped coverage path: {filename}')
    return name


def arcs(data, sources, *, checkout=None):
    if not data.has_arcs():
        raise ValueError('branch coverage is required')
    result = {}
    for filename in data.measured_files():
        name = canonical_path(filename, sources, checkout=checkout)
        if name in result:
            raise ValueError(f'canonical coverage path collision: {name}')
        result[name] = set(data.arcs(filename) or ())
    return result


def read_branch_data(path):
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            value = db.execute("SELECT value FROM meta WHERE key='has_arcs'").fetchone()
    except sqlite3.Error as error:
        raise ValueError(f'could not read coverage mode from {path}') from error
    if value != ('1',):
        raise ValueError(f'branch coverage is required in {path}')
    data = CoverageData(basename=str(path))
    data.read()
    if not data.has_arcs():
        raise ValueError(f'coverage mode mismatch in {path}')
    return data


def write_arcs(output, measurements):
    data = CoverageData(basename=str(output))
    data.erase()
    data.add_arcs(measurements)
    data.write()


def combine(root, expected, *, output='.coverage'):
    inputs = sorted(root.glob('*/coverage-provenance.json'))
    if len(inputs) != 2:
        raise ValueError('expected Linux and Windows coverage artifacts')
    union = {}
    platforms = set()
    for metadata in inputs:
        recorded = json.loads(metadata.read_text(encoding='utf-8'))
        platforms.add(recorded.pop('platform'))
        if recorded != expected:
            raise ValueError(f'incompatible coverage provenance: {metadata}')
        data = read_branch_data(metadata.with_name('coverage-data'))
        for name, edges in arcs(data, expected['source_sha256']).items():
            union.setdefault(name, set()).update(edges)
    if platforms != {'Linux', 'Windows'}:
        raise ValueError('expected distinct Linux and Windows reports')
    write_arcs(output, union)


def worker_receive_line():
    lines, start = inspect.getsourcelines(_child)
    for number, line in enumerate(lines, start):
        if line.strip() == 'arguments = connection.recv()':
            return number
    raise ValueError('owned worker coverage marker missing: arguments = connection.recv()')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('record', 'combine'))
    parser.add_argument('artifacts', nargs='?', type=Path)
    parser.add_argument('--coverage-file', type=Path, default=Path('.coverage'))
    parser.add_argument('--output-directory', type=Path, default=Path('.'))
    args = parser.parse_args()
    try:
        expected = provenance()
        if args.action == 'record':
            data = read_branch_data(args.coverage_file)
            measured = arcs(data, expected['source_sha256'], checkout=Path.cwd())
            sentinel = worker_receive_line()
            if not any(
                sentinel in edge for edge in measured.get('src/applicant/infra/owned_worker.py', ())
            ):
                raise ValueError('owned worker subprocess coverage was not recorded')
            write_arcs(args.output_directory / 'coverage-data', measured)
            (args.output_directory / 'coverage-provenance.json').write_text(
                json.dumps({**expected, 'platform': platform.system()}, sort_keys=True),
                encoding='utf-8',
            )
        else:
            if args.artifacts is None:
                parser.error('combine requires the artifact directory')
            combine(args.artifacts, expected)
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f'{error}\n')


if __name__ == '__main__':
    main()
