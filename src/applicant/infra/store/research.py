"""Schema 8: append-only research with relational analytics projections."""

import json

from ...domain.canonical import canonical_json, content_hash
from ...domain.research import (
    Company,
    CompanyEvent,
    CompanyIdentifier,
    CompanyName,
    JobParty,
    ListingObservation,
    ResearchEvidence,
    ResearchImport,
)
from ...errors import StoreError

RECORDS = {
    'companies': ('company_entities', 'entity_id', Company),
    'names': ('company_names', 'name_id', CompanyName),
    'identifiers': ('company_identifiers', 'identifier_id', CompanyIdentifier),
    'events': ('company_events', 'event_id', CompanyEvent),
    'observations': ('research_job_versions', 'job_version_id', ListingObservation),
    'parties': ('job_parties', 'party_id', JobParty),
    'evidence': ('research_evidence', 'evidence_id', ResearchEvidence),
}
EXTRA = {
    'company_names': ', entity_id TEXT NOT NULL REFERENCES company_entities(id)',
    'company_identifiers': ', entity_id TEXT NOT NULL REFERENCES company_entities(id), provider TEXT NOT NULL, provider_company_id TEXT NOT NULL',
    'company_events': ', subject_id TEXT NOT NULL REFERENCES company_entities(id), related_id TEXT REFERENCES company_entities(id), supersedes TEXT UNIQUE REFERENCES company_events(id)',
    'research_job_versions': ', employer_entity_id TEXT REFERENCES company_entities(id)',
    'job_parties': ', job_version_id TEXT NOT NULL REFERENCES research_job_versions(id), entity_id TEXT REFERENCES company_entities(id)',
    'research_evidence': ', entity_id TEXT REFERENCES company_entities(id), job_version_id TEXT REFERENCES research_job_versions(id)',
}
IMMUTABLE = [v[0] for v in RECORDS.values()] + [
    'company_relationships',
    'technologies',
    'technology_aliases',
    'job_technology_facts',
    'job_compensation_facts',
    'research_snapshots',
    'analytics_members',
    'research_job_groups',
    'research_match_decisions',
    'research_source_lineage',
]
RESEARCH_MIGRATION = (
    *(
        f'CREATE TABLE {table} (id TEXT PRIMARY KEY NOT NULL, payload_hash TEXT NOT NULL, payload TEXT NOT NULL CHECK(json_valid(payload)){EXTRA.get(table, "")})'
        for table, _, _ in RECORDS.values()
    ),
    'CREATE TABLE company_relationships (event_id TEXT PRIMARY KEY REFERENCES company_events(id), subject_id TEXT NOT NULL REFERENCES company_entities(id), related_id TEXT NOT NULL REFERENCES company_entities(id), relationship TEXT NOT NULL)',
    'CREATE TABLE technologies (technology_id TEXT PRIMARY KEY, name TEXT NOT NULL)',
    'CREATE TABLE technology_aliases (alias TEXT NOT NULL, version TEXT NOT NULL, technology_id TEXT NOT NULL REFERENCES technologies(technology_id), PRIMARY KEY(alias,version))',
    'CREATE TABLE job_technology_facts (evidence_id TEXT PRIMARY KEY REFERENCES research_evidence(id), job_version_id TEXT NOT NULL REFERENCES research_job_versions(id), technology_id TEXT NOT NULL REFERENCES technologies(technology_id), requirement TEXT NOT NULL, alternative_group TEXT, negated INTEGER NOT NULL, extractor_version TEXT NOT NULL)',
    'CREATE TABLE job_compensation_facts (evidence_id TEXT PRIMARY KEY REFERENCES research_evidence(id), job_version_id TEXT REFERENCES research_job_versions(id), component TEXT NOT NULL, currency TEXT NOT NULL, period TEXT NOT NULL, basis TEXT NOT NULL, minimum TEXT, maximum TEXT)',
    'CREATE TABLE research_snapshots (id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload_hash TEXT NOT NULL, payload TEXT NOT NULL CHECK(json_valid(payload)))',
    'CREATE TABLE analytics_members (snapshot_id TEXT NOT NULL REFERENCES research_snapshots(id), job_version_id TEXT NOT NULL REFERENCES research_job_versions(id), payload TEXT NOT NULL CHECK(json_valid(payload)), PRIMARY KEY(snapshot_id,job_version_id))',
    'CREATE TABLE research_job_groups (id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, payload TEXT NOT NULL CHECK(json_valid(payload)))',
    'CREATE TABLE research_match_decisions (id TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES research_job_groups(id), payload_hash TEXT NOT NULL, payload TEXT NOT NULL CHECK(json_valid(payload)))',
    'CREATE TABLE research_source_lineage (decision_id TEXT NOT NULL REFERENCES research_match_decisions(id), job_version_id TEXT NOT NULL REFERENCES research_job_versions(id), provider TEXT NOT NULL, source_url TEXT NOT NULL, syndication_origin TEXT, PRIMARY KEY(decision_id,job_version_id))',
    'CREATE TABLE provider_health (provider TEXT PRIMARY KEY, status TEXT NOT NULL, failures INTEGER NOT NULL DEFAULT 0, eligible_at TEXT, lease_until TEXT, lease_token TEXT, reason TEXT)',
    'CREATE INDEX research_technology_jobs ON job_technology_facts(job_version_id,technology_id)',
    'CREATE INDEX research_evidence_company ON research_evidence(entity_id,job_version_id)',
    'CREATE INDEX research_party_jobs ON job_parties(job_version_id,entity_id)',
    'CREATE INDEX research_snapshot_kind ON research_snapshots(kind)',
    *(
        f"CREATE TRIGGER immutable_{table}_{op.lower()} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT, 'immutable research history'); END"
        for table in IMMUTABLE
        for op in ('UPDATE', 'DELETE')
    ),
)


class ResearchRepository:
    def __init__(self, db):
        self.db = db

    def _put(self, table, key, value, columns=None):
        if not self.db.in_transaction:
            raise StoreError('research writes require a transaction')
        payload = canonical_json(value)
        digest = content_hash(value)
        found = self.db.execute(
            f'SELECT payload_hash,payload FROM {table} WHERE id=?', (key,)
        ).fetchone()
        if found:
            if found != (digest, payload):
                raise StoreError(f'{table} identity already contains different data: {key}')
            return False
        fields = {'id': key, 'payload_hash': digest, 'payload': payload, **(columns or {})}
        self.db.execute(
            f'INSERT INTO {table} ({",".join(fields)}) VALUES ({",".join("?" for _ in fields)})',
            tuple(fields.values()),
        )
        return True

    def import_bundle(self, bundle: ResearchImport):
        bundle = ResearchImport.model_validate_json(canonical_json(bundle))
        count = 0
        for attribute, (table, identity, _) in RECORDS.items():
            seen = set()
            for value in getattr(bundle, attribute):
                key = getattr(value, identity)
                if key in seen:
                    raise ValueError('duplicate record identity in import')
                seen.add(key)
                columns = {}
                if table in EXTRA:
                    names = {
                        'company_names': ('entity_id',),
                        'company_identifiers': ('entity_id', 'provider', 'provider_company_id'),
                        'company_events': ('subject_id', 'related_id', 'supersedes'),
                        'research_job_versions': ('employer_entity_id',),
                        'job_parties': ('job_version_id', 'entity_id'),
                        'research_evidence': ('entity_id', 'job_version_id'),
                    }[table]
                    columns = {name: getattr(value, name) for name in names}
                if not self._put(table, key, value, columns):
                    continue
                count += 1
                if isinstance(value, CompanyEvent) and value.related_id:
                    self.db.execute(
                        'INSERT INTO company_relationships VALUES (?,?,?,?)',
                        (key, value.subject_id, value.related_id, value.event_type),
                    )
                if isinstance(value, ResearchEvidence):
                    self._facts(value)
        return count

    def _facts(self, evidence):
        fact = evidence.payload
        if fact.kind == 'technology':
            from ...domain.research_analytics import technology_name

            tech = technology_name(fact.technology)
            if not self.db.execute(
                'SELECT 1 FROM technologies WHERE technology_id=?', (tech,)
            ).fetchone():
                self.db.execute('INSERT INTO technologies VALUES (?,?)', (tech, tech))
            alias = fact.technology.casefold().strip()
            if not self.db.execute(
                'SELECT 1 FROM technology_aliases WHERE alias=? AND version=?', (alias, 'tech-1')
            ).fetchone():
                self.db.execute(
                    'INSERT INTO technology_aliases VALUES (?,?,?)', (alias, 'tech-1', tech)
                )
            self.db.execute(
                'INSERT INTO job_technology_facts VALUES (?,?,?,?,?,?,?)',
                (
                    evidence.evidence_id,
                    evidence.job_version_id,
                    tech,
                    fact.requirement,
                    fact.alternative_group,
                    int(fact.negated),
                    evidence.extractor_version,
                ),
            )
        elif fact.kind == 'compensation':
            self.db.execute(
                'INSERT INTO job_compensation_facts VALUES (?,?,?,?,?,?,?,?)',
                (
                    evidence.evidence_id,
                    evidence.job_version_id,
                    fact.component,
                    fact.currency,
                    fact.period,
                    fact.basis,
                    str(fact.minimum) if fact.minimum is not None else None,
                    str(fact.maximum) if fact.maximum is not None else None,
                ),
            )

    def all(self, attribute):
        table, _, model = RECORDS[attribute]
        result = []
        for digest, payload in self.db.execute(
            f'SELECT payload_hash,payload FROM {table} ORDER BY id'
        ).fetchall():
            value = model.model_validate_json(payload)
            if content_hash(value) != digest:
                raise StoreError('research payload integrity mismatch')
            result.append(value)
        return result

    def snapshot(self, key):
        row = self.db.execute(
            'SELECT payload_hash,payload FROM research_snapshots WHERE id=?', (key,)
        ).fetchone()
        if row is None:
            raise StoreError(f'research snapshot not found: {key}')
        value = json.loads(row[1])
        if content_hash(value) != row[0]:
            raise StoreError('snapshot integrity mismatch')
        return value

    def save_snapshot(self, value):
        groups = value.get('groups', value.get('manifest', {}).get('job_groups', []))
        for group in groups:
            group_id = group['job_group_id']
            self._put(
                'research_job_groups',
                group_id,
                {'group_id': group_id, 'identity_basis': group['identity_basis']},
            )
            if self._put(
                'research_match_decisions', group['match_revision'], group, {'group_id': group_id}
            ):
                for job_id in group['job_version_ids']:
                    row = self.db.execute(
                        'SELECT payload FROM research_job_versions WHERE id=?', (job_id,)
                    ).fetchone()
                    if row is None:
                        raise StoreError('match decision references an unknown job version')
                    observation = ListingObservation.model_validate_json(row[0])
                    self.db.execute(
                        'INSERT INTO research_source_lineage VALUES (?,?,?,?,?)',
                        (
                            group['match_revision'],
                            job_id,
                            observation.job.source,
                            observation.source_url,
                            observation.syndication_origin,
                        ),
                    )
        created = self._put(
            'research_snapshots', value['snapshot_id'], value, {'kind': value['kind']}
        )
        if created and value['kind'] == 'technology_analytics':
            for member in value['members']:
                self.db.execute(
                    'INSERT INTO analytics_members VALUES (?,?,?)',
                    (value['snapshot_id'], member['job_version_id'], canonical_json(member)),
                )
        return value
