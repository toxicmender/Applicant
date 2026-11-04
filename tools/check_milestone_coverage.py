"""Require unrounded 90% line/branch coverage for each new M4 runtime module.

uv run --frozen coverage json -o coverage-m4.json
uv run --frozen python tools/check_milestone_coverage.py coverage-m4.json
"""

from __future__ import annotations

import argparse
import ast
import json
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MODULES = (
    'cli/feedback.py',
    'cli/evaluate.py',
    'cli/personalize.py',
    'cli/benchmark.py',
    'cli/company_evidence.py',
    'domain/feedback.py',
    'domain/evaluation.py',
    'domain/personalization.py',
    'domain/benchmarks.py',
    'domain/benchmark_normalization.py',
    'domain/eurostat.py',
    'domain/bls.py',
    'domain/mospi.py',
    'domain/company_evidence.py',
    'infra/eurostat.py',
    'infra/bls.py',
    'infra/mospi.py',
    'infra/company_sources.py',
    'infra/store/feedback.py',
    'infra/store/personalization.py',
    'infra/store/benchmarks.py',
    'infra/store/company_evidence.py',
    'services/feedback.py',
    'services/evaluation.py',
    'services/personalization.py',
    'services/benchmarks.py',
    'services/benchmark_acquisition.py',
    'services/company_evidence.py',
)


def protocol_exclusions(source: str) -> set[int]:
    """Coverage.py excludes Protocol declarations automatically; approve type stubs only."""
    for node in ast.parse(source).body:
        if not isinstance(node, ast.ClassDef) or node.name != 'BenchmarkProvider':
            continue
        if (
            node.decorator_list
            or node.keywords
            or len(node.bases) != 1
            or not isinstance(node.bases[0], ast.Name)
            or node.bases[0].id != 'Protocol'
        ):
            return set()
        for member in node.body:
            if (
                isinstance(member, ast.Expr)
                and isinstance(member.value, ast.Constant)
                and isinstance(member.value.value, str)
            ):
                continue
            if (
                not isinstance(member, ast.FunctionDef)
                or member.decorator_list
                or member.args.defaults
                or any(d is not None for d in member.args.kw_defaults)
                or len(member.body) != 1
                or not isinstance(member.body[0], ast.Expr)
                or not isinstance(member.body[0].value, ast.Constant)
                or member.body[0].value.value is not Ellipsis
            ):
                return set()
        assert node.end_lineno is not None
        # Coverage's ellipsis regex can include trailing whitespace and an EOF
        # sentinel line. Never extend over another statement or comment.
        end = node.end_lineno
        lines = source.splitlines()
        while end < len(lines) and not lines[end].strip():
            end += 1
        if end == len(lines):
            end += 1
        return set(range(node.lineno, end + 1))
    return set()


def check(report: dict) -> dict:
    if report.get('meta', {}).get('branch_coverage') is not True:
        raise ValueError('M4 coverage requires branch-enabled coverage data')
    files = {}
    for name, value in report['files'].items():
        normalized = name.replace('\\', '/')
        if normalized in files:
            raise ValueError('duplicate normalized coverage path')
        files[normalized] = value
    approved = protocol_exclusions(
        (ROOT / 'src' / 'applicant' / 'domain' / 'benchmarks.py').read_text(encoding='utf-8')
    )
    modules, failures = [], []
    for name in MODULES:
        path = 'src/applicant/' + name
        value = files.get(path)
        if value is None:
            failures.append(path + ': missing coverage data')
            continue
        excluded = value.get('excluded_lines', [])
        allowed = approved if name == 'domain/benchmarks.py' else set()
        if set(excluded) - allowed:
            failures.append(path + ': unapproved excluded lines in M4 module')
        summary = value['summary']
        keys = ('num_statements', 'covered_lines', 'num_branches', 'covered_branches')
        counts = [summary[key] for key in keys]
        if any(type(n) is not int or n < 0 for n in counts):
            raise ValueError('invalid coverage counts')
        statements, lines, branches, covered_branches = counts
        if lines > statements or covered_branches > branches or statements + branches == 0:
            raise ValueError('inconsistent or empty coverage counts')
        covered, total = lines + covered_branches, statements + branches
        percent = Fraction(100 * covered, total)
        modules.append(
            {
                'path': path,
                'covered': covered,
                'total': total,
                'percent': round(float(percent), 4),
                'excluded_lines': excluded,
            }
        )
        if percent < 90:
            failures.append(f'{path}: {covered}/{total} statements and branches, below 90%')
    return {
        'kind': 'milestone_module_coverage',
        'schema_version': 1,
        'policy': 'm4-lines-and-branches-v1',
        'minimum': 90,
        'success': not failures,
        'modules': modules,
        'failures': failures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    args = parser.parse_args(argv)
    try:
        result = check(json.loads(args.report.read_text(encoding='utf-8')))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        print(json.dumps({'success': False, 'error': str(error)}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
