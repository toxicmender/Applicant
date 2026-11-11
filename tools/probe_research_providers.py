"""Explicit, bounded live probes; record failures without making CI depend on sites."""

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from applicant.domain.providers import provider
from applicant.infra.provider_registry import board_client


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--export')
    parser.add_argument('--provider', action='append')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--candidate', type=Path)
    mode.add_argument('--release-report', type=Path)
    args = parser.parse_args()
    if args.release_report:
        from live_requisition import release_eligible

        if args.provider or args.export:
            parser.error('--release-report checks an existing artifact')
        try:
            report = json.loads(args.release_report.read_text(encoding='utf-8'))
            sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
            verified = isinstance(report, dict) and release_eligible(report, sha)
        except (OSError, ValueError):
            parser.error('invalid release artifact or checkout')
        print('verified' if verified else 'pending')
        return 0 if verified else 1
    if not args.export:
        parser.error('--export is required for acquisition')
    if args.candidate:
        from live_requisition import verify

        if args.provider:
            parser.error('--candidate supplies its own providers')
        try:
            candidate = json.loads(args.candidate.read_text(encoding='utf-8'))
            sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
            result = verify(
                candidate, sha, session=os.environ.pop('APPLICANT_LIVE_LINKEDIN_STATE', None)
            )
        except (ValueError, OSError):
            parser.error('invalid candidate, session input or checkout')
        Path(args.export).write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
        return {'verified': 0, 'blocked': 3, 'failed': 1}[result['status']]
    outcomes = []
    for name in args.provider or [
        'weworkremotely',
        'wellfound',
        'builtin',
        'welcometothejungle',
        'arc',
        'instahyre',
    ]:
        client = None
        try:
            client = board_client(name, headless=True)
            jobs = client.search('', limit=1)
            outcomes.append(
                {
                    'provider': name,
                    'status': 'readable' if jobs else 'empty',
                    'samples': [job.model_dump(mode='json') for job in jobs],
                    'requisition_metadata': getattr(client, 'requisition_metadata', {}),
                }
            )
        except Exception as error:  # noqa: BLE001 - operational probe records environment/provider failures
            outcomes.append(
                {
                    'provider': name,
                    'status': 'unavailable',
                    'error_type': type(error).__name__,
                    'fallback': 'research import',
                    'url': provider(name).url,
                }
            )
        finally:
            if client:
                client.close()
    result = {
        'kind': 'live_provider_validation',
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'outcomes': outcomes,
        'cross_board_gate': 'not_verified',
        'note': 'Individual source readability does not verify the same requisition on two boards.',
    }
    Path(args.export).write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + '\n', encoding='utf-8'
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
