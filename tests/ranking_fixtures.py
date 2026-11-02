"""Fresh, deterministic inputs shared by ranking tests; no live providers."""

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from applicant.domain.preferences import DIMENSIONS
from applicant.domain.ranking_facts import CompensationFacts
from applicant.domain.ranking_v2 import (
    CriterionResult,
    DimensionResult,
    EligibilityDecision,
    JobRankingV2,
)

NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / 'fixtures' / 'ranking'


def compensation_fixture() -> dict:
    return json.loads((FIXTURES / 'compensation.json').read_text(encoding='utf-8'))


def compensation_facts(**changes) -> CompensationFacts:
    fixture = compensation_fixture()
    data = fixture['cases'][0]['facts'] | {'source': fixture['source']}
    return CompensationFacts.model_validate(data | changes)


def unknown_result(job_id: str = 'job', version_id: str = 'version') -> JobRankingV2:
    return JobRankingV2(
        job_id=job_id,
        job_version_id=version_id,
        decision=EligibilityDecision(
            criteria=(
                CriterionResult(
                    criterion='compensation_floor',
                    status='unknown',
                    reason_code='salary_unknown',
                    explanation='Base pay was not stated.',
                ),
            )
        ),
        dimensions=tuple(DimensionResult(dimension=d, status='unknown') for d in DIMENSIONS),
        confidence=Decimal(0),
        coverage=Decimal(0),
    )
