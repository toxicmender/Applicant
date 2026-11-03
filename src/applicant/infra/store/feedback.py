"""Append-only candidate feedback with run membership and revision checks."""

import sqlite3
from datetime import datetime

from ...domain.canonical import canonical_json
from ...domain.feedback import FeedbackEvent, FeedbackInput
from ...errors import StoreError
from .profiles import RevisionConflict
from .ranking_common import decode, require_transaction, validated
from .ranking_runs import RankingRunRepository

FEEDBACK_MIGRATION = (
    """CREATE TABLE feedback_events (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        run_id TEXT NOT NULL REFERENCES run_completions(ranking_run_id),
        job_id TEXT NOT NULL, job_version_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision > 0),
        payload TEXT NOT NULL CHECK(json_valid(payload)),
        UNIQUE(run_id, job_id, revision),
        FOREIGN KEY(run_id, job_id, job_version_id)
          REFERENCES run_inputs(ranking_run_id, job_id, job_version_id))""",
    'CREATE INDEX feedback_run_sequence ON feedback_events(run_id, sequence)',
    *(
        f'CREATE TRIGGER immutable_feedback_{op.lower()} BEFORE {op} ON feedback_events '
        "BEGIN SELECT RAISE(ABORT, 'immutable feedback history'); END"
        for op in ('UPDATE', 'DELETE')
    ),
)


class FeedbackRepository:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    @staticmethod
    def _decode(row) -> FeedbackEvent:
        event = decode(FeedbackEvent, row[6])
        if row[:6] != (
            event.sequence,
            event.request.event_id,
            event.request.run_id,
            event.request.job_id,
            event.job_version_id,
            event.revision,
        ):
            raise StoreError('feedback identity does not match stored keys')
        return event

    def history(self, run_id: str, *, through: int | None = None) -> tuple[FeedbackEvent, ...]:
        rows = self.db.execute(
            'SELECT sequence,event_id,run_id,job_id,job_version_id,revision,payload '
            'FROM feedback_events WHERE run_id=? AND (? IS NULL OR sequence<=?) ORDER BY sequence',
            (run_id, through, through),
        ).fetchall()
        events = tuple(self._decode(row) for row in rows)
        revisions = {}
        run = RankingRunRepository(self.db).get(run_id)
        members = dict(
            self.db.execute(
                'SELECT job_id,job_version_id FROM run_inputs WHERE ranking_run_id=?', (run_id,)
            )
        )
        for event in events:
            job_id = event.request.job_id
            if (
                event.revision != revisions.get(job_id, 0) + 1
                or members.get(job_id) != event.job_version_id
                or (event.profile_id, event.profile_revision)
                != (run.profile_id, run.profile_revision)
            ):
                raise StoreError(
                    'feedback history is inconsistent with run membership or revisions'
                )
            revisions[job_id] = event.revision
        return events

    def append(self, request: FeedbackInput, *, recorded_at: datetime) -> FeedbackEvent:
        require_transaction(self.db)
        request = validated(request)
        existing = self.db.execute(
            'SELECT sequence,event_id,run_id,job_id,job_version_id,revision,payload '
            'FROM feedback_events WHERE event_id=?',
            (request.event_id,),
        ).fetchone()
        if existing:
            event = self._decode(existing)
            if event.request != request:
                raise RevisionConflict('feedback event ID already contains a different request')
            self.history(request.run_id)
            return event
        run = RankingRunRepository(self.db).get(request.run_id)
        member = self.db.execute(
            'SELECT job_version_id FROM run_inputs WHERE ranking_run_id=? AND job_id=?',
            (request.run_id, request.job_id),
        ).fetchone()
        if member is None:
            raise ValueError('feedback job is not a member of the completed run')
        history = self.history(request.run_id)
        current = next(
            (e.revision for e in reversed(history) if e.request.job_id == request.job_id), 0
        )
        if request.expected_revision != current:
            raise RevisionConflict(
                f'expected feedback revision {request.expected_revision}; current revision is {current}'
            )
        # Writer lock serializes allocation, including rolled-back batch imports.
        sequence = self.db.execute(
            'SELECT coalesce(max(sequence),0)+1 FROM feedback_events'
        ).fetchone()[0]
        event = FeedbackEvent(
            sequence=sequence,
            request=request,
            revision=current + 1,
            job_version_id=member[0],
            profile_id=run.profile_id,
            profile_revision=run.profile_revision,
            recorded_at=recorded_at,
        )
        self.db.execute(
            'INSERT INTO feedback_events VALUES (?,?,?,?,?,?,?)',
            (
                sequence,
                request.event_id,
                request.run_id,
                request.job_id,
                member[0],
                event.revision,
                canonical_json(event),
            ),
        )
        return event
