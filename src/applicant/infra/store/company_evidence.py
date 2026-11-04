"""Schema 7: immutable verified associations and self-contained evidence captures."""

from ...domain.canonical import canonical_json, content_hash
from ...domain.company_evidence import CompanyAssociation, CompanyEvidenceSnapshot
from ...errors import StoreError
from .job_versions import JobVersionRepository
from .ranking_common import decode, require_transaction, validated

COMPANY_EVIDENCE_MIGRATION = (
    """CREATE TABLE company_associations (
        association_id TEXT PRIMARY KEY NOT NULL, job_id TEXT NOT NULL, job_version_id TEXT NOT NULL,
        provider TEXT NOT NULL, provider_company_id TEXT NOT NULL,
        payload_hash TEXT NOT NULL, payload TEXT NOT NULL CHECK(json_valid(payload)),
        UNIQUE(association_id,provider,provider_company_id),
        FOREIGN KEY(job_id,job_version_id) REFERENCES job_versions(job_id,job_version_id))""",
    """CREATE TABLE company_evidence_snapshots (
        snapshot_id TEXT PRIMARY KEY NOT NULL, association_id TEXT NOT NULL,
        provider TEXT NOT NULL, provider_company_id TEXT NOT NULL,
        snapshot_hash TEXT NOT NULL, payload TEXT NOT NULL CHECK(json_valid(payload)),
        FOREIGN KEY(association_id,provider,provider_company_id)
          REFERENCES company_associations(association_id,provider,provider_company_id))""",
    'CREATE INDEX company_association_versions ON company_associations(job_id,job_version_id)',
    'CREATE INDEX company_snapshot_associations ON company_evidence_snapshots(association_id,provider,provider_company_id)',
    *(
        f'CREATE TRIGGER immutable_{table}_{op.lower()} BEFORE {op} ON {table} '
        "BEGIN SELECT RAISE(ABORT, 'immutable company evidence'); END"
        for table in ('company_associations', 'company_evidence_snapshots')
        for op in ('UPDATE', 'DELETE')
    ),
)


class CompanyEvidenceNotFound(StoreError):
    pass


class CompanyEvidenceRepository:
    def __init__(self, db):
        self.db = db

    def association(self, key: str) -> CompanyAssociation:
        row = self.db.execute(
            'SELECT job_id,job_version_id,provider,provider_company_id,payload_hash,payload FROM company_associations WHERE association_id=?',
            (key,),
        ).fetchone()
        if row is None:
            raise CompanyEvidenceNotFound(f'company association not found: {key}')
        value = decode(CompanyAssociation, row[5])
        if (
            value.association_id != key
            or (
                value.job_id,
                value.job_version_id,
                value.provider,
                value.provider_company_id,
                content_hash(value),
            )
            != row[:5]
        ):
            raise StoreError('company association payload/key integrity mismatch')
        return value

    def snapshot(self, key: str) -> CompanyEvidenceSnapshot:
        row = self.db.execute(
            'SELECT association_id,provider,provider_company_id,snapshot_hash,payload FROM company_evidence_snapshots WHERE snapshot_id=?',
            (key,),
        ).fetchone()
        if row is None:
            raise CompanyEvidenceNotFound(f'company evidence snapshot not found: {key}')
        value = decode(CompanyEvidenceSnapshot, row[4])
        if (
            value.snapshot_id != key
            or (
                value.association_id,
                value.provider,
                value.provider_company_id,
                value.snapshot_hash,
            )
            != row[:4]
        ):
            raise StoreError('company evidence payload/key integrity mismatch')
        try:
            self.validate_snapshot(value)
        except ValueError as error:
            raise StoreError(f'invalid historical company evidence association: {error}') from error
        return value

    def validate_association(self, value):
        job = JobVersionRepository(self.db).get(value.job_version_id)
        if job.job_id != value.job_id:
            raise ValueError('company association does not match the exact job version')

    def validate_snapshot(self, value):
        association = self.association(value.association_id)
        if (association.provider, association.provider_company_id) != (
            value.provider,
            value.provider_company_id,
        ):
            raise ValueError('company evidence does not match the selected association')
        if value.acquired_at < association.verified_at:
            raise ValueError('company acquisition precedes association verification')

    def put_association(self, value: CompanyAssociation):
        require_transaction(self.db)
        value = validated(value)
        self.validate_association(value)
        if self.db.execute(
            'SELECT 1 FROM company_associations WHERE association_id=?', (value.association_id,)
        ).fetchone():
            if self.association(value.association_id) != value:
                raise StoreError('association ID already contains different inputs')
            return value
        self.db.execute(
            'INSERT INTO company_associations VALUES (?,?,?,?,?,?,?)',
            (
                value.association_id,
                value.job_id,
                value.job_version_id,
                value.provider,
                value.provider_company_id,
                content_hash(value),
                canonical_json(value),
            ),
        )
        return self.association(value.association_id)

    def put_snapshot(self, value: CompanyEvidenceSnapshot):
        require_transaction(self.db)
        value = validated(value)
        self.validate_snapshot(value)
        if self.db.execute(
            'SELECT 1 FROM company_evidence_snapshots WHERE snapshot_id=?', (value.snapshot_id,)
        ).fetchone():
            if self.snapshot(value.snapshot_id) != value:
                raise StoreError('company snapshot ID already contains different inputs')
            return value
        self.db.execute(
            'INSERT INTO company_evidence_snapshots VALUES (?,?,?,?,?,?)',
            (
                value.snapshot_id,
                value.association_id,
                value.provider,
                value.provider_company_id,
                value.snapshot_hash,
                canonical_json(value),
            ),
        )
        return self.snapshot(value.snapshot_id)
