"""Preview and explicitly resolve a conflicting managed export."""

import csv
from pathlib import Path

from ...domain.canonical import content_hash
from ...errors import ConfigError, ExportConflict
from .export_backup import verified_backup
from .financials_lock import export_lock
from .financials_merge import _document, merge_financial_exports
from .managed_merge import keep_settled, merge_export, parse_applications, parse_listing
from .publication import digest, read_bytes
from .sqlite import Store, _now


def resolve_export(database, target, choice, confirmation=None):
    target = Path(target).resolve()
    database = Path(database).resolve()
    if choice not in ('sqlite', 'file'):
        raise ConfigError('choose sqlite or file')
    if target in {database, *(Path(str(database) + suffix) for suffix in ('-wal', '-shm'))}:
        raise ConfigError('export must not replace the database or its sidecars')
    with export_lock(target, wait=confirmation is not None):
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
        if discarded is not None:
            verified_backup(target, backup, current, discarded)
        if read_bytes(target) != current:
            raise ExportConflict(f'{target}: export changed during backup; preview again')
        selected = intended if choice == 'sqlite' else current
        with Store(database) as store, store._transaction() as db:
            db.execute('BEGIN IMMEDIATE')
            if _inspect(store, target, choice)[0]['token'] != confirmation:
                raise ExportConflict(f'{target}: export changed during backup; preview again')
            if kind == 'applications':
                store._replace_managed(db, kind, name, selected)
            if choice == 'file':
                if kind == 'financials':
                    store._replace_financials(db, name, _document(selected))
                elif kind == 'ratings':
                    import json

                    for rating in _ratings(selected):
                        payload = json.dumps(rating, ensure_ascii=False)
                        db.execute(
                            'INSERT INTO ratings (company, source, fetched_at, payload) '
                            'SELECT ?,?,?,? WHERE NOT EXISTS '
                            '(SELECT 1 FROM ratings WHERE company=? AND source=? AND payload=?)',
                            (
                                rating['company'].lower(),
                                rating['source'],
                                _now(),
                                payload,
                                rating['company'].lower(),
                                rating['source'],
                                payload,
                            ),
                        )
                elif kind != 'applications':
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
                (
                    confirmation,
                    kind,
                    name,
                    choice,
                    str(backup) if discarded is not None else '',
                    _now(),
                ),
            )
        with Store(database) as store:
            if kind == 'financials':
                store._reconcile_financials(name, target)
            else:
                store._reconcile_export(kind, name, target)
        return {
            **preview,
            'status': 'resolved',
            'backup': str(backup) if discarded is not None else None,
        }


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
    current = read_bytes(target)
    current_digest = digest(current)
    snapshot_restore = False
    if not pending and current is not None:
        name = store._managed_name('applications', target)
        snapshot = store._application_snapshot(name)
        if snapshot is not None:
            try:
                parse_applications(snapshot)
            except ValueError as error:
                raise ExportConflict(
                    f'{target}: invalid committed application CSV snapshot'
                ) from error
            try:
                parse_applications(current)
            except ValueError:
                snapshot_restore = True
                pending.append(('applications', name, None, None, snapshot, digest(snapshot)))
    if len(pending) != 1:
        raise ExportConflict(f'{target}: expected exactly one pending managed export')
    kind, _, baseline, baseline_digest, intended, intended_digest = pending[0]
    removed = current is None and kind == 'applications' and baseline is not None
    if not removed and (current is None or current_digest in (baseline_digest, intended_digest)):
        raise ExportConflict(f'{target}: no divergent file; rerun normal recovery')
    if removed and choice == 'file':
        raise ExportConflict(f'{target}: cannot accept missing application export')
    conflict = 'ratings export differs from its baseline'
    try:
        if kind == 'financials':
            merge_financial_exports(target, baseline, current, intended)
        elif removed:
            raise ExportConflict(f'{target}: application export was removed')
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
                _ratings(current)
        except (ValueError, csv.Error) as error:
            raise ExportConflict(f'{target}: cannot accept file: {error}') from error
    preview = {
        'status': 'export_conflict',
        'path': str(target),
        'kind': kind,
        'choice': choice,
        'source': 'snapshot' if snapshot_restore else 'pending_export',
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
                snapshot_restore,
            ]
        ),
    }
    return preview, pending[0], current


def _ratings(raw):
    import json

    from ...services.company_evidence import rating_payload

    value = json.loads(raw.decode('utf-8'))
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list) or not value:
        raise ValueError('expected at least one company rating')
    for item in value:
        rating_payload(item)
    return value
