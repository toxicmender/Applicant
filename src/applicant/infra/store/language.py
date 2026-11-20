"""Append-only projections for sourced role-language assertions."""

LANGUAGE_MIGRATION = (
    'CREATE UNIQUE INDEX research_evidence_job_identity ON research_evidence(id, job_version_id)',
    """CREATE TABLE job_language_assertions (
        evidence_id TEXT PRIMARY KEY NOT NULL,
        job_version_id TEXT NOT NULL REFERENCES research_job_versions(id),
        clause_id TEXT NOT NULL,
        member_id TEXT NOT NULL,
        action TEXT NOT NULL CHECK(action IN ('assert', 'retire')),
        UNIQUE(evidence_id, job_version_id, clause_id, member_id),
        FOREIGN KEY(evidence_id, job_version_id) REFERENCES research_evidence(id, job_version_id)
    )""",
    'CREATE INDEX language_member_history ON job_language_assertions(job_version_id, clause_id, member_id)',
    """CREATE TABLE language_supersessions (
        successor_id TEXT NOT NULL,
        predecessor_id TEXT NOT NULL,
        job_version_id TEXT NOT NULL,
        clause_id TEXT NOT NULL,
        member_id TEXT NOT NULL,
        PRIMARY KEY(successor_id, predecessor_id),
        CHECK(successor_id <> predecessor_id),
        FOREIGN KEY(successor_id, job_version_id, clause_id, member_id)
            REFERENCES job_language_assertions(evidence_id, job_version_id, clause_id, member_id),
        FOREIGN KEY(predecessor_id, job_version_id, clause_id, member_id)
            REFERENCES job_language_assertions(evidence_id, job_version_id, clause_id, member_id)
    )""",
    'CREATE INDEX language_superseded_by ON language_supersessions(predecessor_id)',
    *(
        f"""CREATE TRIGGER immutable_{table}_{operation.lower()}
            BEFORE {operation} ON {table}
            BEGIN SELECT RAISE(ABORT, 'immutable language history'); END"""
        for table in ('job_language_assertions', 'language_supersessions')
        for operation in ('UPDATE', 'DELETE')
    ),
)
