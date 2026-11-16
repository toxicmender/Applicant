"""Preview and explicitly resolve a conflicting managed export."""

import csv
from pathlib import Path

from ...domain.canonical import content_hash
from ...errors import ExportConflict
from ...files import write_document_bytes
from .financials_lock import export_lock
from .financials_merge import _document, merge_financial_exports
from .managed_merge import keep_settled, merge_export, parse_applications, parse_listing
from .publication import digest, durable_replace, read_bytes
from .sqlite import Store, _now


def resolve_export(database, target, choice, confirmation=None):
    target = Path(target).resolve()
    database = Path(database).resolve()
    with export_lock(target):
        with Store.open_readonly(database) as store, store.read_snapshot():
            info = _inspect(store, target, choice)
        preview, pending, current = info
        if confirmation is None:
            return preview
        if confirmation != preview['token']:
            raise ExportConflict(
                f'{target}: export changed or confirmation does not match; preview again'
            )
        kind, name, _, _, intended, _ = pending
        discarded = current if choice == 'sqlite' else intended
        backup = target.with_name(f'.{target.name}.conflict-{confirmation}')
        if backup.exists() and read_bytes(backup) != discarded:
            raise ExportConflict(f'{backup}: existing backup differs')
        if not backup.exists():
            write_document_bytes(backup, discarded, replace=durable_replace)
        selected = intended if choice == 'sqlite' else current
        with Store(database) as store, store._transaction() as db:
            if choice == 'file':
                if kind == 'financials':
                    store._replace_financials(db, name, _document(selected))
                else:
                    store._replace_managed(db, kind, name, selected)
            if kind == 'financials':
                db.execute(
                    'UPDATE financial_pending_exports SET baseline=?, baseline_digest=?, '
                    'intended=?, intended_digest=? WHERE document=?',
                    (current, digest(current), selected, digest(selected), name),
                )
            else:
                store._stage_export(db, kind, name, current, selected)
            db.execute(
                'INSERT OR IGNORE INTO export_resolutions VALUES (?, ?, ?, ?, ?, ?)',
                (confirmation, kind, name, choice, str(backup), _now()),
            )
        with Store(database) as store:
            if kind == 'financials':
                store._reconcile_financials(name, target)
            else:
                store._reconcile_export(kind, name, target)
        return {**preview, 'status': 'resolved', 'backup': str(backup)}


def _inspect(store, target, choice):
    pending = []
    name = store._financials_name(target)
    pending.extend(
        store.db.execute(
            "SELECT 'financials', document, baseline, baseline_digest, intended, intended_digest "
            'FROM financial_pending_exports WHERE document=?',
            (name,),
        )
    )
    for kind in ('jobs', 'applications', 'ratings'):
        name = store._managed_name(kind, target)
        pending.extend(
            store.db.execute(
                'SELECT kind, document, baseline, baseline_digest, intended, intended_digest '
                'FROM pending_exports WHERE kind=? AND document=?',
                (kind, name),
            )
        )
    if len(pending) != 1:
        raise ExportConflict(f'{target}: expected exactly one pending managed export')
    kind, _, baseline, baseline_digest, intended, intended_digest = pending[0]
    current = read_bytes(target)
    current_digest = digest(current)
    if current is None or current_digest in (baseline_digest, intended_digest):
        raise ExportConflict(f'{target}: no divergent file; rerun normal recovery')
    conflict = 'ratings export differs from its baseline'
    try:
        if kind == 'financials':
            merge_financial_exports(target, baseline, current, intended)
        elif kind != 'ratings':
            merge_export(kind, target, baseline, current, intended)
    except ExportConflict as error:
        conflict = str(error)
    else:
        if kind != 'ratings':
            raise ExportConflict(f'{target}: these changes can be merged by normal recovery')
    if choice == 'file':
        try:
            if kind == 'jobs':
                parse_listing(current)
            elif kind == 'applications':
                keep_settled(parse_applications(intended), parse_applications(current))
            elif kind == 'financials':
                _document(current)
            else:
                raise ValueError('ratings history is append-only; choose sqlite')
        except (ValueError, csv.Error) as error:
            raise ExportConflict(f'{target}: cannot accept file: {error}') from error
    preview = {
        'status': 'export_conflict',
        'path': str(target),
        'kind': kind,
        'choice': choice,
        'file_sha256': current_digest,
        'sqlite_sha256': intended_digest,
        'conflict': conflict,
        'token': content_hash(
            [
                str(store.path),
                str(target),
                kind,
                baseline_digest,
                current_digest,
                intended_digest,
                choice,
            ]
        ),
    }
    return preview, pending[0], current
