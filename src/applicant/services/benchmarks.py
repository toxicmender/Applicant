"""Offline benchmark imports, frozen reports and explicit job/cohort comparisons."""

from decimal import localcontext
from pathlib import Path
from typing import Any

from ..domain.benchmark_normalization import cohort_groups, normalize_observation
from ..domain.benchmarks import BenchmarkComparisonDocument, BenchmarkSnapshot
from ..domain.canonical import content_hash
from ..domain.ranking import RankingRun
from ..domain.ranking_math import arithmetic_context
from ..infra.store.benchmarks import BenchmarkNotFound, BenchmarkRepository
from ..infra.store.ranking_common import validated, write_transaction
from ..infra.store.sqlite import Store
from .ranking_history import load_history


def import_snapshot(
    database: Path, snapshot: BenchmarkSnapshot, *, dry_run: bool = False
) -> BenchmarkSnapshot:
    snapshot = validated(snapshot)
    if dry_run:
        if database.exists():
            with Store.open_readonly(database) as reader, reader.read_snapshot():
                BenchmarkRepository(reader.db).existing(snapshot)
        return snapshot
    with Store(database) as writer, write_transaction(writer.db):
        return BenchmarkRepository(writer.db).put(snapshot)


def show_snapshots(database: Path, snapshot_ids: tuple[str, ...]) -> dict[str, Any]:
    if not snapshot_ids or len(set(snapshot_ids)) != len(snapshot_ids):
        raise ValueError('select unique benchmark snapshot IDs')
    with Store.open_readonly(database) as reader, reader.read_snapshot():
        snapshots = tuple(BenchmarkRepository(reader.db).get(key) for key in snapshot_ids)
    return {
        'kind': 'benchmark_report',
        'schema_version': 1,
        'snapshots': [s.model_dump() for s in snapshots],
        'cohorts': cohort_groups(snapshots),
        'input_digest': content_hash(snapshots),
    }


def compare_benchmarks(database: Path, run_id: str, document: BenchmarkComparisonDocument) -> dict[str, Any]:
    document = validated(document)
    with Store.open_readonly(database) as reader, reader.read_snapshot():
        history = load_history(reader, run_id)
        if isinstance(history.run, RankingRun):
            raise ValueError('benchmark comparison requires a modern engine run')
        snapshots = {
            key: BenchmarkRepository(reader.db).get(key)
            for key in dict.fromkeys(s.snapshot_id for s in document.selections)
        }
    results = {r.job_id: r for r in history.run.results}
    observations = {
        key: {o.observation_id: o for o in snapshot.observations}
        for key, snapshot in snapshots.items()
    }
    rows = []
    with localcontext(arithmetic_context()):
        for selection in document.selections:
            if selection.job_id not in results:
                raise ValueError('selected job is not a member of the ranking run')
            observation = observations[selection.snapshot_id].get(selection.observation_id)
            if observation is None:
                raise BenchmarkNotFound(f'observation not in snapshot: {selection.observation_id}')
            normalized = normalize_observation(
                observation,
                target_currency=history.profile.salary_currency,
                fx=history.fx,
                annual_hours=selection.annual_hours,
                annual_days=selection.annual_days,
            )
            reasons = list(normalized['reasons'])
            if observation.cohort != selection.expected_cohort:
                reasons.append('cohort_mismatch')
            if observation.cohort.wage_definition != 'base_pay':
                reasons.append('wage_definition_mismatch')
            if observation.cohort.basis != history.profile.salary_basis:
                reasons.append('salary_basis_mismatch')
            pay = results[selection.job_id].compensation
            amount = (
                pay.representative_value if pay is not None and pay.status == 'available' else None
            )
            if amount is None:
                reasons.append('job_compensation_unavailable')
            benchmark = normalized['annual_amount']
            comparable = not reasons
            rows.append(
                {
                    'job_id': selection.job_id,
                    'job_version_id': results[selection.job_id].job_version_id,
                    'snapshot_id': selection.snapshot_id,
                    'snapshot_hash': snapshots[selection.snapshot_id].snapshot_hash,
                    'observation': observation.model_dump(),
                    'selection': selection.model_dump(),
                    'normalized': normalized,
                    'job_annual_amount': amount,
                    'comparable': comparable,
                    'reasons': reasons,
                    'gap': amount - benchmark if comparable else None,
                    'ratio': amount / benchmark if comparable and benchmark else None,
                }
            )
    return {
        'kind': 'benchmark_comparison_report',
        'schema_version': 1,
        'run_id': run_id,
        'normalization_policy': 'benchmark-normalization-v1',
        'results': rows,
        'input_digest': content_hash(
            {
                'run': history.run,
                'profile': history.profile,
                'fx': history.fx,
                'selections': document,
                'snapshots': snapshots,
            }
        ),
        'notice': 'Cohort mappings are caller assertions. Nominal conversion does not adjust tax, purchasing power or reference-year prices.',
    }
