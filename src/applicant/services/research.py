"""Research imports and offline snapshot reads, independent of ranking history."""

import sqlite3
from pathlib import Path

from ..domain.canonical import content_hash
from ..domain.contracts import now
from ..domain.research import ResearchImport
from ..infra.store.research import RECORDS, ResearchRepository
from ..infra.store.sqlite import Store


def import_research(database, bundle, *, dry_run=False):
    bundle = ResearchImport.model_validate(bundle)
    if dry_run:
        # Dry runs must not initialize or migrate the target database.
        db = sqlite3.connect(':memory:')
        try:
            if Path(database).exists():
                with Store.open_readonly(database) as source:
                    source.db.backup(db)
            else:
                from ..infra.store.sqlite import MIGRATIONS

                for migration in MIGRATIONS:
                    for statement in migration:
                        db.execute(statement)
                db.commit()
            db.execute('PRAGMA foreign_keys = ON')
            db.execute('PRAGMA recursive_triggers = ON')
            db.execute('BEGIN')
            count = ResearchRepository(db).import_bundle(bundle)
            db.rollback()
        finally:
            db.close()
    else:
        with Store(database) as store, store.db:
            store.db.execute('BEGIN IMMEDIATE')
            count = ResearchRepository(store.db).import_bundle(bundle)
    return {'kind': 'research_import_result', 'inserted': count, 'dry_run': dry_run}


def read_research(database):
    with Store.open_readonly(database) as store, store.read_snapshot():
        repo = ResearchRepository(store.db)
        return {key: repo.all(key) for key in RECORDS}


def save_snapshot(database, value):
    value = {**value, 'snapshot_id': content_hash(value)}
    with Store(database) as store, store.db:
        store.db.execute('BEGIN IMMEDIATE')
        ResearchRepository(store.db).save_snapshot(value)
    return value


def read_snapshot(database, snapshot_id):
    with Store.open_readonly(database) as store, store.read_snapshot():
        return ResearchRepository(store.db).snapshot(snapshot_id)


def template(kind):
    stamp = now().isoformat()
    company = {
        'entity_id': 'example-company',
        'name': 'Example Company',
        'recorded_at': stamp,
        'source_url': 'https://example.com/company',
    }
    observation = {
        'job_version_id': 'example-job-v1',
        'job': {
            'source': 'import',
            'id': 'example-job',
            'title': 'Software Engineer',
            'company': 'Example Company',
        },
        'source_url': 'https://example.com/jobs/123',
        'acquired_at': stamp,
        'observed_at': stamp,
        'content_sha256': content_hash('Replace with the digest of the captured posting'),
        'employer_entity_id': 'example-company',
        'status': 'open',
    }
    bundle = {'companies': [company]}
    if kind in ('company_event', 'corporate-event'):
        bundle['events'] = [
            {
                'event_id': 'example-rename',
                'event_type': 'rename',
                'subject_id': 'example-company',
                'recorded_at': stamp,
                'source_url': company['source_url'],
                'note': 'Replace with verified rename evidence',
            }
        ]
    elif kind in ('job_party', 'job-party'):
        bundle['observations'] = [observation]
        bundle['parties'] = [
            {
                'party_id': 'example-party',
                'job_version_id': 'example-job-v1',
                'role': 'hiring_employer',
                'entity_id': 'example-company',
                'source_url': observation['source_url'],
                'excerpt': 'Replace with verified employer attribution',
            }
        ]
    else:
        payloads = {
            'compensation': {
                'kind': 'compensation',
                'currency': 'USD',
                'minimum': '100000',
                'maximum': '120000',
                'period': 'annual',
                'basis': 'gross',
                'component': 'base',
            },
            'culture': {
                'kind': 'culture',
                'topic': 'culture',
                'description': 'Replace with a sourced observation',
            },
            'policy': {
                'kind': 'policy',
                'modes': ['Remote'],
                'description': 'Replace with a scoped and dated policy',
            },
            'technology': {'kind': 'technology', 'technology': 'Python', 'requirement': 'required'},
        }
        if kind not in payloads:
            raise ValueError('unknown template kind')
        bundle['observations'] = [observation]
        bundle['evidence'] = [
            {
                'evidence_id': 'example-evidence',
                'entity_id': 'example-company',
                'job_version_id': 'example-job-v1',
                'provider': 'import',
                'source_url': observation['source_url'],
                'acquired_at': stamp,
                'scope': 'job',
                'excerpt': 'Replace with source excerpt; unknown observation dates stay null',
                'payload': payloads[kind],
            }
        ]
    return ResearchImport.model_validate(bundle).model_dump(mode='json')
