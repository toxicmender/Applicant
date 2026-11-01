"""Migration 3: append-only ranking inputs and completed runs.

Only profile_heads is mutable. Completion is an append-only marker inserted last
in the caller's transaction; completed runs cannot gain inputs or results.
"""

IMMUTABLE = (
    'candidate_profiles',
    'fx_snapshots',
    'scoring_configs',
    'job_versions',
    'ranking_runs',
    'run_inputs',
    'job_rankings',
    'run_completions',
)

RANKING_MIGRATION = (
    """CREATE TABLE candidate_profiles (
        profile_id TEXT NOT NULL, profile_revision INTEGER NOT NULL CHECK(profile_revision >= 1),
        payload TEXT NOT NULL CHECK(json_valid(payload)),
        PRIMARY KEY(profile_id, profile_revision))""",
    """CREATE TABLE profile_heads (
        profile_id TEXT NOT NULL PRIMARY KEY, active_revision INTEGER NOT NULL,
        FOREIGN KEY(profile_id, active_revision) REFERENCES candidate_profiles(profile_id, profile_revision))""",
    """CREATE TABLE fx_snapshots (
        snapshot_id TEXT NOT NULL PRIMARY KEY,
        payload TEXT NOT NULL CHECK(json_valid(payload)))""",
    """CREATE TABLE scoring_configs (
        scoring_config_id TEXT NOT NULL PRIMARY KEY, config_hash TEXT NOT NULL UNIQUE,
        payload TEXT NOT NULL CHECK(json_valid(payload)))""",
    """CREATE TABLE job_versions (
        job_version_id TEXT NOT NULL PRIMARY KEY, job_id TEXT NOT NULL,
        content_hash TEXT NOT NULL, ranking_hash TEXT NOT NULL,
        payload TEXT NOT NULL CHECK(json_valid(payload)),
        UNIQUE(job_id, content_hash), UNIQUE(job_id, job_version_id))""",
    """CREATE TABLE ranking_runs (
        ranking_run_id TEXT NOT NULL PRIMARY KEY, profile_id TEXT NOT NULL,
        profile_revision INTEGER NOT NULL, fx_snapshot_id TEXT NOT NULL,
        scoring_config_id TEXT NOT NULL, payload TEXT NOT NULL CHECK(json_valid(payload)),
        FOREIGN KEY(profile_id, profile_revision) REFERENCES candidate_profiles(profile_id, profile_revision),
        FOREIGN KEY(fx_snapshot_id) REFERENCES fx_snapshots(snapshot_id),
        FOREIGN KEY(scoring_config_id) REFERENCES scoring_configs(scoring_config_id))""",
    """CREATE TABLE run_inputs (
        ranking_run_id TEXT NOT NULL REFERENCES ranking_runs(ranking_run_id),
        ordinal INTEGER NOT NULL CHECK(ordinal >= 0), job_id TEXT NOT NULL, job_version_id TEXT NOT NULL,
        PRIMARY KEY(ranking_run_id, job_id), UNIQUE(ranking_run_id, ordinal),
        UNIQUE(ranking_run_id, job_id, job_version_id),
        FOREIGN KEY(job_id, job_version_id) REFERENCES job_versions(job_id, job_version_id))""",
    """CREATE TABLE job_rankings (
        ranking_run_id TEXT NOT NULL, job_id TEXT NOT NULL, job_version_id TEXT NOT NULL,
        overall_score REAL CHECK(overall_score BETWEEN 0 AND 100),
        confidence REAL CHECK(confidence BETWEEN 0 AND 1),
        payload TEXT NOT NULL CHECK(json_valid(payload)),
        PRIMARY KEY(ranking_run_id, job_id),
        FOREIGN KEY(ranking_run_id, job_id, job_version_id)
          REFERENCES run_inputs(ranking_run_id, job_id, job_version_id))""",
    """CREATE TABLE run_completions (
        ranking_run_id TEXT NOT NULL PRIMARY KEY REFERENCES ranking_runs(ranking_run_id))""",
    'CREATE INDEX ranking_profile_history ON ranking_runs(profile_id, profile_revision)',
    'CREATE INDEX job_ranking_hash ON job_versions(job_id, ranking_hash)',
    *(
        f'CREATE TRIGGER immutable_{table}_{operation.lower()} BEFORE {operation} ON {table} '
        "BEGIN SELECT RAISE(ABORT, 'immutable ranking history'); END"
        for table in IMMUTABLE
        for operation in ('UPDATE', 'DELETE')
    ),
    *(
        f'CREATE TRIGGER completed_{table} BEFORE INSERT ON {table} '
        'WHEN EXISTS (SELECT 1 FROM run_completions WHERE ranking_run_id = NEW.ranking_run_id) '
        "BEGIN SELECT RAISE(ABORT, 'run already completed'); END"
        for table in ('run_inputs', 'job_rankings')
    ),
)
