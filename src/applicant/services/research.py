"""Research imports and offline snapshot reads, independent of ranking history."""

import sqlite3
from pathlib import Path
from typing import Any

from ..domain.canonical import content_hash
from ..domain.contracts import now
from ..domain.language import singleton
from ..domain.ranking import Evidence, JobVersion
from ..domain.ranking_inputs import JobVersionBundle
from ..domain.research import ListingObservation, parse_import
from ..infra.store.research import RECORDS, ResearchRepository
from ..infra.store.sqlite import Store


def import_research(database, bundle, *, dry_run=False):
    bundle = parse_import(bundle)
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
            repo = ResearchRepository(db)
            count = repo.import_bundle(bundle)
            members = repo.resolved_members(bundle)
            db.rollback()
        finally:
            db.close()
    else:
        with Store(database) as store, store.db:
            store.db.execute('BEGIN IMMEDIATE')
            repo = ResearchRepository(store.db)
            count = repo.import_bundle(bundle)
            members = repo.resolved_members(bundle)
    return {
        'kind': 'research_import_result',
        'inserted': count,
        'dry_run': dry_run,
        **({'resolved_members': members} if members else {}),
    }


def export_job_versions(database, selection):
    if (
        not isinstance(selection, dict)
        or set(selection) != {'job_version_ids'}
        or not isinstance(selection['job_version_ids'], list)
        or not selection['job_version_ids']
        or any(not isinstance(key, str) or not key for key in selection['job_version_ids'])
    ):
        raise ValueError('selection requires a nonempty job_version_ids array')
    jobs = []
    with Store.open_readonly(database) as store, store.read_snapshot():
        repo = ResearchRepository(store.db)
        for observation_id in selection['job_version_ids']:
            row = store.db.execute(
                'SELECT payload FROM research_job_versions WHERE id=?', (observation_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f'unknown research job version {observation_id}')
            observation = ListingObservation.model_validate_json(row[0])
            assertion_evidence = repo.active_languages(observation_id)
            stamp = max(
                (item.acquired_at for item in assertion_evidence), default=observation.acquired_at
            )
            stamp = max(stamp, observation.acquired_at)
            payload = {
                'kind': 'research_language_context',
                'schema_version': 1,
                'observation_id': observation_id,
                'assertions': [item.model_dump(mode='json') for item in assertion_evidence],
            }
            source = Evidence(
                source=observation.job.source,
                observed_at=stamp,
                extractor_version='research-language-1',
                payload=payload,
            )
            job = JobVersion.capture(
                observation.job,
                parser_version='research-1',
                evidence=(source,),
            )
            jobs.append(
                job.model_copy(
                    update={
                        'captured_at': stamp,
                        'job_version_id': content_hash(
                            {'observation': observation_id, 'job': job.content_hash}
                        ),
                    }
                )
            )
    if len({job.job_id for job in jobs}) != len(jobs):
        raise ValueError('select exactly one research version per job')
    return JobVersionBundle(kind='ranking_job_versions', schema_version=1, jobs=tuple(jobs))


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
    bundle: dict[str, Any] = {'companies': [company]}
    if kind in ('company_event', 'corporate-event'):
        bundle['schema_version'] = 2
        bundle['events'] = [
            {
                'event_id': 'example-rename',
                'schema_version': 2,
                'effective_at': stamp,
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
            'language': {
                'kind': 'language',
                'clause_id': 'example-english',
                'member_id': singleton('example-job-v1', 'example-english'),
                'language': 'English',
                'skill': 'overall',
                'strength': 'required',
                'threshold': {'kind': 'level', 'scale': 'cefr', 'level': 'B2'},
            },
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
    return parse_import(bundle).model_dump(mode='json')
