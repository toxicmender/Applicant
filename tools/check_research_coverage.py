"""Require separate, unrounded 90% statement and branch coverage."""

import argparse
import json
from pathlib import Path

MODULES = (
    'cli/setup.py',
    'cli/sources.py',
    'domain/research_analytics.py',
    'services/provider_setup.py',
    'services/research_feed.py',
)


def check(report):
    if report.get('meta', {}).get('branch_coverage') is not True:
        raise ValueError('branch coverage is required')
    files = {}
    for name, data in report['files'].items():
        name = name.replace('\\', '/')
        if name in files:
            raise ValueError('duplicate normalized path')
        files[name] = data
    failures, modules = [], []
    for name in MODULES:
        path = 'src/applicant/' + name
        if path not in files:
            failures.append(path + ': missing coverage')
            continue
        item = files[path]
        summary = item['summary']
        counts = [
            summary[key]
            for key in ('num_statements', 'covered_lines', 'num_branches', 'covered_branches')
        ]
        if any(type(n) is not int or n < 0 for n in counts):
            raise ValueError('invalid coverage counts')
        statements, lines, branches, covered = counts
        if not statements or lines > statements or covered > branches:
            raise ValueError('inconsistent coverage counts')
        if item.get('excluded_lines'):
            failures.append(path + ': unapproved exclusions')
        if lines * 10 < statements * 9:
            failures.append(path + ': line coverage below 90%')
        if branches and covered * 10 < branches * 9:
            failures.append(path + ': branch coverage below 90%')
        modules.append(
            {
                'path': path,
                'lines': [lines, statements],
                'branches': [covered, branches],
                'missing_lines': item.get('missing_lines', []),
                'missing_branches': item.get('missing_branches', []),
            }
        )
    return {'success': not failures, 'minimum': 90, 'modules': modules, 'failures': failures}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    args = parser.parse_args()
    try:
        result = check(json.loads(args.report.read_text(encoding='utf-8')))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        result = {'success': False, 'error': str(error)}
    print(json.dumps(result, sort_keys=True))
    return int(not result['success'])


if __name__ == '__main__':
    raise SystemExit(main())
