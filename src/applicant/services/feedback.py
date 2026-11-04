"""Candidate feedback imports and read-only snapshots."""

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from ..domain.feedback import FeedbackDocument, FeedbackEvent
from ..infra.store.feedback import FeedbackRepository
from ..infra.store.ranking_common import validated, write_transaction
from ..infra.store.ranking_runs import RankingRunRepository
from ..infra.store.sqlite import Store


def import_feedback(
    database: Path, document: FeedbackDocument, *, recorded_at: datetime, dry_run: bool = False
) -> tuple[FeedbackEvent, ...]:
    document = validated(document)
    # Simulate on a private in-memory copy before opening any durable writer.
    with Store.open_readonly(database) as reader, closing(sqlite3.connect(':memory:')) as temporary:
        reader.db.backup(temporary)
        temporary.execute('PRAGMA foreign_keys=ON')
        with write_transaction(temporary):
            proposed = tuple(
                FeedbackRepository(temporary).append(r, recorded_at=recorded_at)
                for r in document.events
            )
    if dry_run:
        return proposed
    with Store(database) as writer, write_transaction(writer.db):
        return tuple(
            FeedbackRepository(writer.db).append(r, recorded_at=recorded_at)
            for r in document.events
        )


def feedback_report(
    database: Path, run_id: str, *, history: bool = False, through: int | None = None
) -> dict[str, Any]:
    with Store.open_readonly(database) as reader, reader.read_snapshot():
        RankingRunRepository(reader.db).get(run_id)
        watermark = reader.db.execute(
            'SELECT coalesce(max(sequence),0) FROM feedback_events'
        ).fetchone()[0]
        if through is not None and not 0 <= through <= watermark:
            raise ValueError('feedback cutoff must be within recorded history')
        events = FeedbackRepository(reader.db).history(run_id, through=through)
    effective = {e.request.job_id: e for e in events}
    return {
        'kind': 'candidate_feedback_report',
        'schema_version': 1,
        'run_id': run_id,
        'feedback_through': through
        if through is not None
        else max((e.sequence for e in events), default=0),
        'events': [e.model_dump() for e in events]
        if history
        else [effective[key].model_dump() for key in sorted(effective)],
    }
