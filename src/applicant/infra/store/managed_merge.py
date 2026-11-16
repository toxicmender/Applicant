"""Validate and merge managed JSON listings and application CSV exports."""

import csv
import io
import json

from ...domain.dedupe import key
from ...errors import ExportConflict
from ...files import document_bytes
from ...storage import APPLIED_COLUMNS, is_final, restore, write_rows
from .financials_merge import _merge


def parse_listing(raw):
    if raw is None:
        return {'list': []}
    data = json.loads(raw.decode('utf-8'))
    if not isinstance(data, dict) or not isinstance(data.get('list', []), list):
        raise ValueError('expected a job listing')
    if any(not isinstance(item, dict) for item in data.get('list', [])):
        raise ValueError('expected job objects')
    return {**data, 'list': data.get('list', [])}


def parse_applications(raw):
    if raw is None:
        return []
    reader = csv.DictReader(io.StringIO(raw.decode('utf-8-sig'), newline=''), strict=True)
    if reader.fieldnames != APPLIED_COLUMNS:
        raise ValueError('application CSV columns do not match')
    rows = []
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            raise ValueError('invalid application CSV row')
        rows.append({column: restore(value) for column, value in row.items()})
    return rows


def keep_settled(old, new):
    missing = [row for row in old if is_final(row) and row not in new]
    if missing:
        raise ValueError('file would remove or change settled application outcomes')


def merge_export(kind, target, baseline, current, intended):
    try:
        if kind == 'applications':
            base, edited, saved = (parse_applications(raw) for raw in (baseline, current, intended))
            keep_settled(base, edited)
            if edited[: len(base)] != base or saved[: len(base)] != base:
                raise ValueError('application edits must be disjoint appends')
            # Existing duplicate attempts can share a posting ID. Competing
            # appended outcomes for the same posting need operator review.
            additions = saved[len(base) :]
            for row in edited[len(base) :]:
                if row in additions:
                    continue
                if any(key(other) == key(row) for other in additions):
                    raise ValueError('conflicting appended application outcomes')
                additions.append(row)
            output = io.StringIO(newline='')
            write_rows(output, [*base, *additions], header=True)
            return output.getvalue().encode('utf-8-sig')

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
