"""Record and combine compatible coverage artifacts from the native CI jobs."""

import argparse
import hashlib
import inspect
import json
import platform
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from coverage import Coverage, CoverageData, __version__

from applicant.infra.owned_worker import _child


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
    }


def canonical_path(filename, sources):
    name = filename.replace('\\', '/')
    for marker in ('/work/Applicant/Applicant/', '/a/Applicant/Applicant/'):
        if marker in name:
            name = name.split(marker, 1)[1]
            break
    if name not in sources:
        raise ValueError(f'unmapped coverage path: {filename}')
    return name


def arcs(data, sources):
    if not data.has_arcs():
        raise ValueError('branch coverage is required')
    result = {}
    for filename in data.measured_files():
        name = canonical_path(filename, sources)
        if name in result:
            raise ValueError(f'canonical coverage path collision: {name}')
        result[name] = set(data.arcs(filename) or ())
    return result


def combine(root, expected, *, output='.coverage'):
    inputs = sorted(root.glob('*/coverage-provenance.json'))
    if len(inputs) != 2:
        raise ValueError('expected Linux and Windows coverage artifacts')
    union = {}
    platforms = set()
    with TemporaryDirectory() as temp:
        for index, metadata in enumerate(inputs):
            recorded = json.loads(metadata.read_text(encoding='utf-8'))
            platforms.add(recorded.pop('platform'))
            if recorded != expected:
                raise ValueError(f'incompatible coverage provenance: {metadata}')
            source = metadata.with_name('coverage-data')
            data = CoverageData(basename=str(source))
            data.read()
            for name, edges in arcs(data, expected['source_sha256']).items():
                union.setdefault(name, set()).update(edges)
            shutil.copyfile(source, Path(temp) / f'.coverage.{index}')
        if platforms != {'Linux', 'Windows'}:
            raise ValueError('expected distinct Linux and Windows reports')
        combined = Coverage(data_file=output)
        combined.combine(data_paths=[temp], strict=True)
        if arcs(combined.get_data(), expected['source_sha256']) != union:
            raise ValueError('coverage combine dropped or duplicated arcs')
        combined.save()


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
    args = parser.parse_args()
    try:
        expected = provenance()
        if args.action == 'record':
            data = CoverageData()
            data.read()
            measured = arcs(data, expected['source_sha256'])
            sentinel = worker_receive_line()
            if not any(
                sentinel in edge for edge in measured.get('src/applicant/infra/owned_worker.py', ())
            ):
                raise ValueError('owned worker subprocess coverage was not recorded')
            shutil.copyfile('.coverage', 'coverage-data')
            Path('coverage-provenance.json').write_text(
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
