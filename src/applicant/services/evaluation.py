"""Read-only comparisons and evaluation over one consistent database snapshot."""

from pathlib import Path
from typing import Any

from ..domain.evaluation import compare_runs, evaluate_feedback
from ..domain.ranking import RankingRun
from ..domain.ranking_report import ranking_report
from ..domain.ranking_v2 import ScoringConfigurationV2
from ..infra.store.feedback import FeedbackRepository
from ..infra.store.sqlite import Store
from .ranking_history import load_history


def evaluate_run(database: Path, run_id: str, *, top: int = 20, through: int | None = None) -> dict[str, Any]:
    with Store.open_readonly(database) as reader, reader.read_snapshot():
        history = load_history(reader, run_id)
        if isinstance(history.run, RankingRun) or not isinstance(
            history.config, ScoringConfigurationV2
        ):
            raise ValueError('evaluation requires a modern engine run')
        watermark = reader.db.execute(
            'SELECT coalesce(max(sequence),0) FROM feedback_events'
        ).fetchone()[0]
        cutoff: int = int(watermark) if through is None else through
        if cutoff < 0 or cutoff > watermark:
            raise ValueError('feedback cutoff must be within recorded history')
        events = FeedbackRepository(reader.db).history(run_id, through=cutoff)
        return evaluate_feedback(history.run, history.config, events, top=top, through=cutoff)


def compare_history(database: Path, base_id: str, candidate_id: str) -> dict[str, Any]:
    with Store.open_readonly(database) as reader, reader.read_snapshot():
        reports = []
        inputs = []
        for run_id in (base_id, candidate_id):
            history = load_history(reader, run_id)
            report = ranking_report(history.run, history.jobs, config=history.config, top=None)
            if 'legacy' in report:
                identities = {j.job_version_id: j.job_id for j in history.jobs}
                for item in report['legacy']['results']:
                    item['job_id'] = identities[item['job_version_id']]
            reports.append(report)
            inputs.append(
                {
                    'profile': history.profile.model_dump(),
                    'configuration': history.config.model_dump(),
                    'fx': history.fx.model_dump(),
                }
            )
    report = compare_runs(*reports)
    report['input_changes'] = {
        key: {'base': inputs[0][key], 'candidate': inputs[1][key]}
        for key in inputs[0]
        if inputs[0][key] != inputs[1][key]
    }
    return report
