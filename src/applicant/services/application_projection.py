"""Publish journal outcomes without reapplying a committed change."""

import base64
import re

from ..domain.job import Job
from ..domain.ports import UNCONFIRMED
from ..errors import ExportConflict
from ..financials.tracker import _utc_timestamp
from ..infra.store.application_csv import ApplicationCSV
from ..infra.store.publication import digest, read_bytes, reconcile
from ..storage import application_row

_TAG = re.compile(r'^\[applicant-attempt-v1:([^\]]+)\]')
STATUSES = {'submitted': 'applied', 'not-submitted': 'not_submitted', 'abandoned': 'abandoned'}


def projection_bytes(raw, attempt, entry):
    layout = ApplicationCSV(raw)
    rows = layout.rows()
    job = Job.model_validate(entry['job'])
    state = STATUSES[entry['state']]
    marker = f'[applicant-attempt-v1:{attempt}]'
    tagged = [
        index for index, row in enumerate(rows) if row.get('note', '').split(' ', 1)[0] == marker
    ]
    if len(tagged) > 1:
        raise ExportConflict(f'duplicate tagged application rows for {attempt}')
    if tagged:
        index = tagged[0]
    elif entry.get('synthetic'):
        index = None
    else:
        matches = [
            idx
            for idx, row in enumerate(rows)
            if row['source'] == job.source
            and row['id'] == job.id
            and not _TAG.match(row['note'])
            and (row['note'] == UNCONFIRMED or (state == 'applied' and row['status'] == 'applied'))
        ]
        if len(matches) <= 1:
            index = matches[0] if matches else None
        else:
            observed = _milliseconds(entry.get('recorded_at'))
            exact = [
                idx
                for idx in matches
                if observed is not None and _milliseconds(rows[idx].get('applied_at')) == observed
            ]
            if len(exact) != 1:
                raise ExportConflict(f'ambiguous legacy CSV rows for attempt {attempt}')
            index = exact[0]
    note = f'{marker} {state.replace("_", " ")}'
    if index is None:
        row = application_row(job, state, note)
        row['applied_at'] = entry.get('recorded_at', row['applied_at'])
        layout.set(None, row)
    else:
        layout.set(index, {'status': state, 'note': note})
    return layout.render()


def _milliseconds(value):
    stamp = _utc_timestamp(value)
    if stamp is None:
        return None
    return stamp.replace(microsecond=stamp.microsecond // 1000 * 1000)


def publish_file(target, event, checkpoint):
    def decode(value):
        return None if value is None else base64.b64decode(value, validate=True)

    def encode(value):
        return None if value is None else base64.b64encode(value).decode('ascii')

    if 'intent' not in event:
        baseline = read_bytes(target)
        intended = projection_bytes(baseline, event['attempt'], event['entry'])
        event['intent'] = [encode(baseline), encode(intended)]
        checkpoint()
    baseline, intended = map(decode, event['intent'])
    current = read_bytes(target)
    if current is None and baseline is not None:
        raise ExportConflict(f'{target}: application export was removed after staging')
    if current is not None and current not in (baseline, intended):
        completed, staged = ApplicationCSV(current), ApplicationCSV(intended)
        mark = f'[applicant-attempt-v1:{event["attempt"]}]'

        def matching(csv):
            note = csv.header.index('note')
            return [row for row in csv.cells if row[note].split(' ', 1)[0] == mark]

        saved, expected = matching(completed), matching(staged)
        if completed.header == staged.header and len(expected) == 1 and saved == expected:
            return
    reconcile(target, (baseline, digest(baseline), intended, digest(intended)), lambda _: None)
