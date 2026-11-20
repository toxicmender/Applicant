"""Validate archived company lineage without regenerating version IDs."""

from .ranking import JobVersion


def enrichment_steps(job: JobVersion) -> list[dict]:
    steps: list[dict] = []
    ancestors = {job.job_version_id}
    snapshots = [
        (
            item.payload.get('parent_job_version_id'),
            item.payload.get('snapshot_id'),
            item.payload.get('snapshot_hash'),
        )
        for item in job.evidence
        if item.payload.get('kind') == 'company_evidence_context'
        # The enclosing Evidence.observed_at is the required acquisition time
        # in company-context-1. The payload observation time can be null.
    ]
    for evidence in job.evidence:
        record = evidence.payload
        if record.get('kind') != 'company_enrichment_lineage':
            continue
        chain = record.get('enrichment_steps')
        if (
            record.get('schema_version') != 1
            or not isinstance(chain, list)
            or len(chain) != len(steps) + 1
            or chain[:-1] != steps
            or not isinstance(chain[-1], dict)
        ):
            raise ValueError(f'lineage_prefix_mismatch: {job.job_version_id}')
        step = chain[-1]
        parent = step.get('parent_job_version_id')
        if (
            not isinstance(parent, str)
            or not parent
            or parent != record.get('immediate_parent_job_version_id')
        ):
            raise ValueError(f'lineage_parent_mismatch: {job.job_version_id}')
        if parent in ancestors:
            raise ValueError(f'lineage_cycle: {job.job_version_id}')
        selected = step.get('snapshots')
        if (
            not isinstance(selected, list)
            or not selected
            or any(
                not isinstance(item, dict)
                or not isinstance(item.get('snapshot_id'), str)
                or not isinstance(item.get('snapshot_hash'), str)
                or (parent, item['snapshot_id'], item['snapshot_hash']) not in snapshots
                for item in selected
            )
        ):
            raise ValueError(f'lineage_snapshot_mismatch: {job.job_version_id}')
        ancestors.add(parent)
        steps.append(step)
    return steps
