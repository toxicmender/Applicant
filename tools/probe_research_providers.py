"""Explicit, bounded live probes; record failures without making CI depend on sites."""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from applicant.domain.providers import provider
from applicant.infra.provider_registry import board_client


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--export', required=True)
    parser.add_argument('--provider', action='append')
    args = parser.parse_args()
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
