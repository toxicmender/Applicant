"""Exercise M4 through real offline CLI subprocesses and a fixed golden clock.

uv run --frozen --isolated --no-editable python tools/smoke_milestone4.py --require-installed
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import applicant

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / 'tests' / 'fixtures'


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def read(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


def raw_response(path: Path) -> dict:
    body = path.read_bytes()
    return {
        'source_url': 'https://example.invalid/synthetic-company',
        'body_base64': base64.b64encode(body).decode('ascii'),
        'body_sha256': hashlib.sha256(body).hexdigest(),
        'representation': 'http_response',
    }


def journey(*, require_installed: bool = False) -> dict:
    if require_installed:
        expect(
            not Path(applicant.__file__).resolve().is_relative_to(ROOT / 'src'),
            'M4 journey requires an installed wheel, not editable sources',
        )
    expected = read(FIXTURES / 'milestone4' / 'journey.json')
    environment = dict(os.environ)
    for name in (
        'APPLICANT_HOME',
        'APPLICANT_CONFIG',
        'APPLICANT_STORE',
        'APPLICANT_MONEY_CACHE',
        'CRUNCHBASE_API_KEY',
        'TRACXN_API_KEY',
        'PYTHONPATH',
        'PYTHONSTARTUP',
    ):
        environment.pop(name, None)
    environment['PYTHONIOENCODING'] = 'utf-8'
    with tempfile.TemporaryDirectory(prefix='applicant-m4-') as temporary:
        data = Path(temporary) / 'M4 # 测试 ₹'
        data.mkdir()
        guard = Path(temporary) / 'guard'
        guard.mkdir()
        (guard / 'sitecustomize.py').write_bytes(
            (FIXTURES / 'milestone4' / 'sitecustomize.py').read_bytes()
        )
        environment['PYTHONPATH'] = str(guard)

        def process(arguments: list[str]):
            return subprocess.run(
                [sys.executable, *arguments],
                cwd=data,
                env=environment,
                capture_output=True,
                encoding='utf-8',
                timeout=45,
            )

        probe = process(['-c', "import socket; socket.getaddrinfo('must-not-connect.invalid',443)"])
        expect(
            probe.returncode != 0 and 'network forbidden in Milestone 4' in probe.stderr,
            'subprocess network guard did not load',
        )

        def cli(*args: str, code: int = 0):
            result = process(['-m', 'applicant', '--no-log-file', '--data-dir', str(data), *args])
            expect(
                result.returncode == code,
                f'{args}: exit {result.returncode}, expected {code}\n{result.stderr}\n{result.stdout}',
            )
            return result.stdout

        def report(*args: str):
            return json.loads(cli(*args))

        def write(name: str, value):
            path = data / name
            path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
            return path

        database = data / 'applicant.db'
        report('rank', 'list', '--json')
        cli('company-evidence', 'show', '--snapshot', 'missing', code=1)
        expect(not database.exists(), 'read-only commands created a missing database')
        cli('survey', 'import', str(FIXTURES / 'ranking' / 'preferences.json'))
        cli('db', 'upgrade', '--database', 'applicant.db')
        bundle = read(FIXTURES / 'ranking' / 'cli-versions.json')
        original = bundle['jobs'][0]
        eligible = []
        for name, salary in (('a', '90000'), ('b', '75000'), ('c', '60000')):
            job = copy.deepcopy(original)
            job['job_version_id'] = 'm4-' + name
            job['job_data'].update(id='m4-' + name, title='M4 Engineer ' + name)
            pay = job['evidence'][0]['payload']['compensation'][0]
            pay.update(
                minimum=salary, maximum=salary, original_text=f'EUR {salary} gross base per year'
            )
            eligible.append(job)
        bundle['jobs'] = eligible + bundle['jobs'][1:]
        write('versions.json', bundle)
        provenance = str(FIXTURES / 'ranking' / 'provenance.json')

        def rank(path: str):
            return report(
                'rank',
                'create',
                '--job-versions',
                path,
                '--provenance',
                provenance,
                '--offline',
                '--json',
            )

        base = rank('versions.json')
        run_id = base['run']['ranking_run_id']
        groups = base['groups']
        counts = {name: group['total'] for name, group in groups.items()}
        expect(counts == expected['group_counts'], 'M4 eligibility groups differ from golden data')
        jobs = groups['eligible']['results']
        review = groups['review_required']['results'][0]
        excluded = groups['excluded']['results'][0]
        explanation = report('rank', 'explain', run_id, review['job_id'], '--json')
        conflicts = [c for c in explanation['result']['decision']['criteria'] if c['conflicts']]
        expect(
            bool(conflicts) and all(c['conflicts'][0]['needs_verification'] for c in conflicts),
            'conflict explanation lost its verification prompt',
        )
        empty = report('evaluate', '--run', run_id, '--top', '2')
        expect(
            empty['eligible']['judged'] == 0 and empty['metrics']['precision_at_k'] is None,
            'unknown judgments became negatives',
        )
        expect(
            report('personalize', 'suggest', '--run', run_id)['status'] == 'insufficient_feedback',
            'suggested weights without support',
        )
        importance = [
            '--importance',
            'compensation=more_important',
            '--importance',
            'remote_compatibility=less_important',
        ]
        first_args = (
            'feedback',
            'record',
            '--run',
            run_id,
            '--job',
            jobs[0]['job_id'],
            '--event-id',
            'm4-first',
            '--decision',
            'shortlist',
            '--expected-revision',
            '0',
            *importance,
        )
        first = report(*first_args)
        expect(report(*first_args) == first, 'feedback retry changed the recorded event')
        partial = report('evaluate', '--run', run_id, '--top', '2', '--export', 'partial.json')
        cutoff = partial['feedback_through']

        def event(identity, job, decision, **extra):
            return {
                'event_id': identity,
                'run_id': run_id,
                'job_id': job['job_id'],
                'expected_revision': 0,
                'decision': decision,
                **extra,
            }

        events = [
            event(
                'm4-second',
                jobs[1],
                'shortlist',
                importance={
                    'compensation': 'more_important',
                    'remote_compatibility': 'less_important',
                },
            ),
            event(
                'm4-third',
                jobs[2],
                'dismiss',
                reasons=['compensation'],
                importance={
                    'compensation': 'more_important',
                    'remote_compatibility': 'less_important',
                },
            ),
            event('m4-review', review, 'dismiss', reasons=['conflicting_evidence']),
            event('m4-excluded', excluded, 'dismiss', reasons=['closed_role']),
        ]
        write(
            'feedback.json',
            {'kind': 'candidate_feedback_import', 'schema_version': 1, 'events': events},
        )
        before = database.read_bytes()
        report('feedback', 'import', '--from-file', 'feedback.json', '--dry-run')
        expect(database.read_bytes() == before, 'feedback dry run mutated history')
        report('feedback', 'import', '--from-file', 'feedback.json')
        complete = report('evaluate', '--run', run_id, '--top', '2', '--export', 'complete.json')
        expect(
            complete['other_groups']['review_required']['judged']
            == complete['other_groups']['excluded']['judged']
            == 1,
            'noneligible judgments disappeared or contaminated metrics',
        )
        stale = report('personalize', 'suggest', '--run', run_id)['suggestion']
        cli(
            'feedback',
            'record',
            '--run',
            run_id,
            '--job',
            jobs[0]['job_id'],
            '--event-id',
            'm4-corrected',
            '--decision',
            'shortlist',
            '--expected-revision',
            '1',
            '--notes',
            'Reconfirmed; complete replacement state.',
            *importance,
        )
        before = database.read_bytes()
        cli('personalize', 'accept', '--suggestion', stale['suggestion_id'], code=2)
        expect(database.read_bytes() == before, 'stale suggestion acceptance mutated history')
        suggested = report('personalize', 'suggest', '--run', run_id)['suggestion']
        expect(len(suggested['supporting_event_ids']) == 3, 'suggestion used noneligible judgments')
        expect(
            {p['job_version_id']: p['decision'] for p in suggested['preview']}
            == {r['job_version_id']: r['decision'] for g in groups.values() for r in g['results']},
            'weight preview changed eligibility',
        )
        accepted = report('personalize', 'accept', '--suggestion', suggested['suggestion_id'])
        expect(
            report('personalize', 'accept', '--suggestion', suggested['suggestion_id']) == accepted,
            'acceptance retry created a new revision',
        )
        candidate = rank('versions.json')
        candidate_judged = report('evaluate', '--run', candidate['run']['ranking_run_id'])[
            'eligible'
        ]['judged']
        comparison = report('rank', 'compare', run_id, candidate['run']['ranking_run_id'], '--json')
        expect(
            all(j['membership'] == 'unchanged_version' for j in comparison['jobs'])
            and 'profile' in comparison['input_changes'],
            'weight comparison lost exact membership or changed inputs',
        )

        # Source payloads/identities below are synthetic, never live captures.
        associations, snapshots = [], []
        for provider, company_id, locator, filename in (
            (
                'glassdoor',
                '42',
                'https://www.glassdoor.com/Reviews/Acme-Reviews-E42.htm',
                'rating.json',
            ),
            (
                'crunchbase',
                'acme',
                'https://www.crunchbase.com/organization/acme',
                'financials.json',
            ),
        ):
            associated = {
                'kind': 'company_association',
                'schema_version': 1,
                'association_id': provider,
                'job_id': jobs[0]['job_id'],
                'job_version_id': jobs[0]['job_version_id'],
                'provider': provider,
                'provider_company_id': company_id,
                'locator': locator,
                'verified_at': '2026-11-01T00:00:00Z',
                'verified_by': 'fixture',
                'verification_note': 'Synthetic explicit association for the offline journey.',
                'verification_url': 'https://example.invalid/company',
            }
            associations.append(associated)
            source = FIXTURES / 'company_evidence' / filename
            snapshots.append(
                {
                    'kind': 'company_evidence_snapshot',
                    'schema_version': 1,
                    'snapshot_id': provider + '-snapshot',
                    'association_id': provider,
                    'provider': provider,
                    'provider_company_id': company_id,
                    'acquired_at': '2026-11-02T00:00:00Z',
                    'observed_at': '2026-11-01T00:00:00Z',
                    'observation_note': 'Synthetic source observation date.',
                    'payload': read(source),
                    'responses': [raw_response(source)],
                }
            )
        write('association.json', associations[0])
        report('company-evidence', 'associate', '--from-file', 'association.json')
        write(
            'company.json',
            {
                'kind': 'company_evidence_import',
                'schema_version': 1,
                'associations': associations,
                'snapshots': snapshots,
            },
        )
        before = database.read_bytes()
        report('company-evidence', 'import', '--from-file', 'company.json', '--dry-run')
        expect(database.read_bytes() == before, 'company dry run mutated history')
        report('company-evidence', 'import', '--from-file', 'company.json')
        write(
            'enrichment.json',
            {
                'kind': 'company_enrichment',
                'schema_version': 1,
                'selections': [
                    {'association_id': s['association_id'], 'snapshot_id': s['snapshot_id']}
                    for s in snapshots
                ],
            },
        )
        enriched = report(
            'company-evidence',
            'enrich',
            '--from-file',
            'enrichment.json',
            '--export',
            'enriched.json',
        )
        new_job = enriched['jobs'][0]
        write(
            'enriched-batch.json',
            bundle
            | {
                'jobs': [
                    new_job if j['job_version_id'] == jobs[0]['job_version_id'] else j
                    for j in bundle['jobs']
                ]
            },
        )
        enriched_run = rank('enriched-batch.json')
        changed = report('rank', 'compare', run_id, enriched_run['run']['ranking_run_id'], '--json')
        changed_versions = sum(j['membership'] == 'changed_version' for j in changed['jobs'])
        context = next(
            j
            for j in enriched_run['groups']['eligible']['results']
            if j['job_id'] == jobs[0]['job_id']
        )
        expect(
            next(d for d in context['dimensions'] if d['dimension'] == 'company_culture')['status']
            == 'known',
            'dated company ratings did not reach scoring',
        )
        expect(
            next(d for d in context['dimensions'] if d['dimension'] == 'stability')['status']
            == 'unknown',
            'financial information became a health score',
        )

        benchmark = read(FIXTURES / 'benchmarks' / 'snapshot.json')
        missing_fx = copy.deepcopy(benchmark['observations'][0])
        missing_fx.update(observation_id='missing-fx', currency='USD')
        benchmark['observations'].append(missing_fx)
        write('benchmark.json', benchmark)
        report('benchmark', 'import', '--from-file', 'benchmark.json', '--dry-run')
        report('benchmark', 'import', '--from-file', 'benchmark.json')
        write(
            'cohorts.json',
            {
                'kind': 'benchmark_comparison',
                'schema_version': 1,
                'selections': [
                    {
                        'job_id': jobs[0]['job_id'],
                        'snapshot_id': benchmark['snapshot_id'],
                        'observation_id': o['observation_id'],
                        'expected_cohort': o['cohort'],
                    }
                    for o in benchmark['observations']
                ],
            },
        )
        before = database.read_bytes()
        comparison = report(
            'benchmark',
            'compare',
            '--run',
            run_id,
            '--selections',
            'cohorts.json',
            '--export',
            'benchmarks.json',
        )
        expect(
            next(
                r
                for r in comparison['results']
                if r['observation']['observation_id'] == 'missing-fx'
            )['normalized']['annual_amount']
            is None,
            'benchmark conversion invented an FX rate',
        )
        report('benchmark', 'show', '--snapshot', benchmark['snapshot_id'])
        report(
            'company-evidence',
            'show',
            '--snapshot',
            snapshots[0]['snapshot_id'],
            '--export',
            'company-report.json',
        )
        report('personalize', 'show', '--suggestion', suggested['suggestion_id'])
        history = report('feedback', 'list', '--run', run_id, '--history')
        expect(len(history['events']) == 6, 'feedback correction deleted or duplicated history')
        expect(
            report('evaluate', '--run', run_id, '--top', '2', '--feedback-through', str(cutoff))
            == partial,
            'frozen evaluation changed after later feedback',
        )
        expect(
            report('rank', 'show', run_id, '--json') == base,
            'historical ranking changed after personalization/enrichment',
        )
        historical_replay = report('rank', 'replay', run_id, '--provenance', provenance, '--json')
        enriched_replay = report(
            'rank',
            'replay',
            enriched_run['run']['ranking_run_id'],
            '--provenance',
            provenance,
            '--json',
        )
        completed_runs = len(report('rank', 'list', '--json')['runs'])
        for command in (
            ('evaluate', '--run', run_id),
            ('benchmark', 'show', '--snapshot', benchmark['snapshot_id']),
            ('company-evidence', 'show', '--snapshot', snapshots[0]['snapshot_id']),
        ):
            cli(*command, '--export', 'applicant.db', code=2)
        write('invalid.json', {})
        (data / 'invalid.json').write_text(
            '{"kind":"company_evidence_import","kind":"duplicate"}', encoding='utf-8'
        )
        cli('company-evidence', 'import', '--from-file', 'invalid.json', code=2)
        expect(
            database.read_bytes() == before,
            'reports/exports/rejected operations mutated the database',
        )
        for name in (
            'partial.json',
            'complete.json',
            'enriched.json',
            'benchmarks.json',
            'company-report.json',
        ):
            read(data / name)
            if os.name == 'posix':
                expect(
                    (data / name).stat().st_mode & 0o077 == 0, 'export permissions are not private'
                )
        summary = {
            'group_counts': counts,
            'partial_precision': partial['metrics']['precision_at_k'],
            'partial_precision_lower_bound': partial['metrics']['precision_lower_bound'],
            'partial_precision_upper_bound': partial['metrics']['precision_upper_bound'],
            'complete_precision': complete['metrics']['precision_at_k'],
            'complete_recall': complete['metrics']['recall_at_k'],
            'transfer': suggested['transfer'],
            'recipient': suggested['recipient'],
            'donor': suggested['donor'],
            'accepted_revision': accepted['profile_revision'],
            'candidate_judged': candidate_judged,
            'changed_versions': changed_versions,
            'benchmark_observations': len(benchmark['observations']),
            'comparable_benchmarks': sum(r['comparable'] for r in comparison['results']),
            'context_benchmarks': sum(not r['comparable'] for r in comparison['results']),
            'company_contexts': len(context['company_evidence']),
            'historical_replay_matches': historical_replay['verification']['matches'],
            'enriched_replay_matches': enriched_replay['verification']['matches'],
            'completed_runs': completed_runs,
        }
        expect(
            summary == {k: v for k, v in expected.items() if k != 'fixture_version'},
            'M4 summary differs from golden data: ' + json.dumps(summary, sort_keys=True),
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
