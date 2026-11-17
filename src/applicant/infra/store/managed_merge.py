"""Validate and merge managed JSON listings and application CSV exports."""

import csv
import json

from ...domain.application_cells import sanitize_external
from ...domain.dedupe import key
from ...domain.job import Job
from ...errors import ExportConflict
from ...files import document_bytes
from ...storage import is_final
from .application_csv import ApplicationCSV
from .financials_merge import _merge


def parse_listing(raw):
    if raw is None:
        return {'list': []}
    data = json.loads(raw.decode('utf-8'))
    if not isinstance(data, dict) or not isinstance(data.get('list', []), list):
        raise ValueError('expected a job listing')
    if any(not isinstance(item, dict) for item in data.get('list', [])):
        raise ValueError('expected job objects')
    if len({key(item) for item in data.get('list', [])}) != len(data.get('list', [])):
        raise ValueError('duplicate job keys')
    for item in data.get('list', []):
        Job.model_validate(item.get('pending_job_v1', item))
    return {**data, 'list': data.get('list', [])}


def parse_applications(raw):
    return ApplicationCSV(raw).rows()


def keep_settled(old, new):
    missing = [row for row in old if is_final(row) and row not in new]
    if missing:
        raise ValueError('file would remove or change settled application outcomes')


def merge_export(kind, target, baseline, current, intended):
    try:
        if kind == 'applications':
            base, edited, saved = map(ApplicationCSV, (baseline, current, intended))
            if baseline is None:
                base.header = saved.header
            if base.header != edited.header or base.header != saved.header:
                raise ValueError('application export headers differ')
            start = len(base.cells)
            if edited.cells[:start] != base.cells or saved.cells[:start] != base.cells:
                raise ValueError('application edits must be disjoint appends')
            rows = {tuple(map(sanitize_external, row)) for row in saved.cells[start:]}
            names = (saved.header.index('source'), saved.header.index('id'))
            identities = {(row[names[0]], row[names[1]]) for row in rows}
            for row in edited.cells[start:]:
                sanitized = tuple(map(sanitize_external, row))
                if sanitized in rows:
                    continue
                identity = sanitized[names[0]], sanitized[names[1]]
                if identity in identities:
                    raise ValueError('conflicting appended application outcomes')
                saved.cells.append(list(sanitized))
                rows.add(sanitized)
                identities.add(identity)
            return saved.render()

        base, edited, saved = (parse_listing(raw) for raw in (baseline, current, intended))
        conflicts = []
        base_jobs, edited_jobs, saved_jobs = (
            {key(job): job for job in doc.pop('list')} for doc in (base, edited, saved)
        )
        jobs = _merge(base_jobs, edited_jobs, saved_jobs, 'jobs', conflicts)
        metadata = _merge(base, edited, saved, 'metadata', conflicts)
        if conflicts:
            raise ValueError(', '.join(conflicts))
        return document_bytes({**metadata, 'list': list(jobs.values())})
    except (ValueError, csv.Error) as error:
        raise ExportConflict(f'{target}: {error}; use store resolve-export') from error
