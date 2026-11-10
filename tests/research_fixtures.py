"""Synthetic multi-board research, never presented as live provider evidence."""

from datetime import datetime, timezone

from applicant.domain.canonical import content_hash
from applicant.domain.research import ResearchImport

NOW = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
STAMP = '2026-11-01T12:00:00Z'


def bundle():
    companies = [
        {
            'entity_id': key,
            'name': name,
            'recorded_at': '2025-01-01T00:00:00Z',
            'source_url': f'https://example.com/{key}',
        }
        for key, name in [('startup', 'Café Startup'), ('parent', 'Parent Group')]
    ]
    observations, evidence, parties = [], [], []
    for index in range(6):
        for source in ('linkedin', 'indeed'):
            version = f'{source}-{index}'
            url = f'https://example.com/jobs/{index}'
            observations.append(
                {
                    'job_version_id': version,
                    'job': {
                        'source': source,
                        'id': str(index),
                        'title': 'Software Engineer',
                        'company': 'Café Startup',
                        'location': 'Bengaluru',
                        'url': url,
                    },
                    'acquired_at': STAMP,
                    'observed_at': STAMP,
                    'source_url': url,
                    'content_sha256': content_hash(version),
                    'employer_entity_id': 'startup',
                    'employer_requisition_id': str(index),
                    'origin': 'direct',
                    'status': 'open',
                    'syndication_origin': 'employer-ats',
                }
            )
            parties.append(
                {
                    'party_id': version,
                    'job_version_id': version,
                    'role': 'hiring_employer',
                    'entity_id': 'startup',
                    'source_url': url,
                    'excerpt': 'Direct employer identity explicitly confirmed in synthetic fixture',
                }
            )
            for suffix, payload in [
                (
                    'technology',
                    {'kind': 'technology', 'technology': 'Python', 'requirement': 'required'},
                ),
                (
                    'salary',
                    {
                        'kind': 'compensation',
                        'currency': 'USD',
                        'minimum': str(100000 + index * 1000),
                        'maximum': str(120000 + index * 1000),
                        'period': 'annual',
                        'basis': 'gross',
                        'component': 'base',
                    },
                ),
            ]:
                evidence.append(
                    {
                        'evidence_id': version + '-' + suffix,
                        'entity_id': 'startup',
                        'job_version_id': version,
                        'provider': source,
                        'source_url': url,
                        'acquired_at': STAMP,
                        'observed_at': STAMP,
                        'scope': 'job',
                        'assertion': 'employer',
                        'excerpt': 'Synthetic source excerpt',
                        'payload': payload,
                    }
                )
    evidence.extend(
        [
            {
                'evidence_id': 'old-culture',
                'entity_id': 'startup',
                'provider': 'glassdoor',
                'source_url': 'https://example.com/culture',
                'acquired_at': STAMP,
                'observed_at': '2025-01-01T00:00:00Z',
                'scope': 'company',
                'assertion': 'employee',
                'excerpt': 'Historical culture',
                'payload': {
                    'kind': 'culture',
                    'topic': 'work_life_balance',
                    'description': 'Historical sample, not current policy',
                },
            },
            {
                'evidence_id': 'policy',
                'entity_id': 'startup',
                'provider': 'import',
                'source_url': 'https://example.com/policy',
                'acquired_at': STAMP,
                'observed_at': STAMP,
                'scope': 'company',
                'assertion': 'employer',
                'excerpt': 'Explicit US Eastern working hours',
                'payload': {
                    'kind': 'policy',
                    'modes': ['Remote'],
                    'description': '09:00-17:00 New York; Bengaluru ಬೆಂಗಳೂರು / बेंगलुरु; ₹ € £',
                    'schedule': {
                        'employer_timezone': 'America/New_York',
                        'start': '09:00',
                        'end': '17:00',
                    },
                },
            },
        ]
    )
    return ResearchImport.model_validate(
        {
            'companies': companies,
            'observations': observations,
            'evidence': evidence,
            'parties': parties,
            'names': [
                {
                    'name_id': 'old-name',
                    'entity_id': 'startup',
                    'name': 'Old Startup',
                    'name_type': 'historical',
                    'effective_to': '2026-01-01',
                    'recorded_at': STAMP,
                    'source_url': 'https://example.com/rename',
                }
            ],
            'identifiers': [
                {
                    'identifier_id': 'gd-startup',
                    'entity_id': 'startup',
                    'provider': 'glassdoor',
                    'provider_company_id': '123',
                    'verified_at': STAMP,
                    'verification_url': 'https://example.com/startup',
                }
            ],
            'events': [
                {
                    'event_id': 'acquisition',
                    'event_type': 'acquisition',
                    'subject_id': 'startup',
                    'related_id': 'parent',
                    'effective_date': '2026-06-01',
                    'recorded_at': '2026-06-02T00:00:00Z',
                    'source_url': 'https://example.com/acquisition',
                    'note': 'Synthetic acquisition evidence',
                }
            ],
        }
    )
