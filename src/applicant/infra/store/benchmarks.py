"""Schema 6: immutable completed benchmark snapshots and exact raw responses."""

from ...domain.benchmarks import BenchmarkObservation, BenchmarkResponse, BenchmarkSnapshot
from ...domain.canonical import canonical_json
from ...errors import StoreError
from .ranking_common import decode, require_transaction, validated

BENCHMARK_MIGRATION = (
    """CREATE TABLE benchmark_responses (
        response_id TEXT PRIMARY KEY NOT NULL, payload TEXT NOT NULL CHECK(json_valid(payload)))""",
    """CREATE TABLE benchmark_snapshots (
        snapshot_id TEXT PRIMARY KEY NOT NULL, snapshot_hash TEXT NOT NULL,
        payload TEXT NOT NULL CHECK(json_valid(payload)))""",
    """CREATE TABLE benchmark_snapshot_responses (
        snapshot_id TEXT NOT NULL REFERENCES benchmark_snapshots(snapshot_id),
        response_id TEXT NOT NULL REFERENCES benchmark_responses(response_id),
        ordinal INTEGER NOT NULL CHECK(ordinal>=0),
        PRIMARY KEY(snapshot_id,response_id), UNIQUE(snapshot_id,ordinal))""",
    """CREATE TABLE benchmark_observations (
        snapshot_id TEXT NOT NULL, observation_id TEXT NOT NULL,
        response_id TEXT NOT NULL, ordinal INTEGER NOT NULL CHECK(ordinal>=0),
        payload TEXT NOT NULL CHECK(json_valid(payload)),
        PRIMARY KEY(snapshot_id,observation_id), UNIQUE(snapshot_id,ordinal),
        FOREIGN KEY(snapshot_id,response_id)
          REFERENCES benchmark_snapshot_responses(snapshot_id,response_id))""",
    """CREATE TABLE benchmark_completions (
        snapshot_id TEXT PRIMARY KEY NOT NULL REFERENCES benchmark_snapshots(snapshot_id))""",
    *(
        f'CREATE TRIGGER immutable_{table}_{op.lower()} BEFORE {op} ON {table} '
        "BEGIN SELECT RAISE(ABORT, 'immutable benchmark history'); END"
        for table in (
            'benchmark_responses',
            'benchmark_snapshots',
            'benchmark_snapshot_responses',
            'benchmark_observations',
            'benchmark_completions',
        )
        for op in ('UPDATE', 'DELETE')
    ),
    *(
        f'CREATE TRIGGER completed_{table} BEFORE INSERT ON {table} '
        'WHEN EXISTS (SELECT 1 FROM benchmark_completions WHERE snapshot_id=NEW.snapshot_id) '
        "BEGIN SELECT RAISE(ABORT, 'benchmark already completed'); END"
        for table in ('benchmark_snapshot_responses', 'benchmark_observations')
    ),
)


class BenchmarkNotFound(StoreError):
    pass


class BenchmarkRepository:
    def __init__(self, db):
        self.db = db

    def get(self, snapshot_id: str) -> BenchmarkSnapshot:
        row = self.db.execute(
            'SELECT s.snapshot_hash,s.payload FROM benchmark_snapshots s '
            'JOIN benchmark_completions c USING(snapshot_id) WHERE s.snapshot_id=?',
            (snapshot_id,),
        ).fetchone()
        if row is None:
            raise BenchmarkNotFound(f'completed benchmark not found: {snapshot_id}')
        responses = []
        for i, (response_id, ordinal, payload) in enumerate(
            self.db.execute(
                'SELECT l.response_id,l.ordinal,r.payload FROM benchmark_snapshot_responses l '
                'JOIN benchmark_responses r USING(response_id) WHERE l.snapshot_id=? ORDER BY l.ordinal',
                (snapshot_id,),
            )
        ):
            response = decode(BenchmarkResponse, payload)
            if ordinal != i or response.response_id != response_id:
                raise StoreError('benchmark response identity/order does not match stored keys')
            responses.append(response)
        observations = []
        for i, (observation_id, response_id, ordinal, payload) in enumerate(
            self.db.execute(
                'SELECT observation_id,response_id,ordinal,payload FROM benchmark_observations '
                'WHERE snapshot_id=? ORDER BY ordinal',
                (snapshot_id,),
            )
        ):
            observation = decode(BenchmarkObservation, payload)
            if ordinal != i or (observation.observation_id, observation.response_id) != (
                observation_id,
                response_id,
            ):
                raise StoreError('benchmark observation identity/order does not match stored keys')
            observations.append(observation)
        import json

        try:
            metadata = json.loads(row[1])
            if isinstance(metadata, dict) and {'responses', 'observations'} & metadata.keys():
                raise ValueError('snapshot metadata must not contain child records')
            snapshot = BenchmarkSnapshot.model_validate(
                metadata | {'responses': responses, 'observations': observations}
            )
        except (ValueError, TypeError) as error:
            raise StoreError(f'invalid historical benchmark snapshot: {error}') from error
        if snapshot.snapshot_id != snapshot_id or snapshot.snapshot_hash != row[0]:
            raise StoreError('benchmark snapshot identity/hash does not match stored keys')
        return snapshot

    def existing(self, snapshot: BenchmarkSnapshot) -> BenchmarkSnapshot | None:
        if self.db.execute(
            'SELECT 1 FROM benchmark_snapshots WHERE snapshot_id=?', (snapshot.snapshot_id,)
        ).fetchone():
            existing = self.get(snapshot.snapshot_id)
            if existing != snapshot:
                raise StoreError('benchmark snapshot ID already contains different inputs')
            return existing
        return None

    def put(self, snapshot: BenchmarkSnapshot) -> BenchmarkSnapshot:
        require_transaction(self.db)
        snapshot = validated(snapshot)
        existing = self.existing(snapshot)
        if existing is not None:
            return existing
        self.db.execute(
            'INSERT INTO benchmark_snapshots VALUES (?,?,?)',
            (
                snapshot.snapshot_id,
                snapshot.snapshot_hash,
                canonical_json(snapshot.model_dump(exclude={'responses', 'observations'})),
            ),
        )
        for ordinal, response in enumerate(snapshot.responses):
            self.db.execute(
                'INSERT INTO benchmark_responses VALUES (?,?) ON CONFLICT(response_id) DO NOTHING',
                (response.response_id, canonical_json(response)),
            )
            self.db.execute(
                'INSERT INTO benchmark_snapshot_responses VALUES (?,?,?)',
                (snapshot.snapshot_id, response.response_id, ordinal),
            )
        for ordinal, observation in enumerate(snapshot.observations):
            self.db.execute(
                'INSERT INTO benchmark_observations VALUES (?,?,?,?,?)',
                (
                    snapshot.snapshot_id,
                    observation.observation_id,
                    observation.response_id,
                    ordinal,
                    canonical_json(observation),
                ),
            )
        self.db.execute('INSERT INTO benchmark_completions VALUES (?)', (snapshot.snapshot_id,))
        return self.get(snapshot.snapshot_id)
