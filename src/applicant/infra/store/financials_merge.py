"""Three-way reconciliation of financials records and their export metadata."""

import json

from ...errors import ExportConflict

_MISSING = object()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate key {key!r}')
        result[key] = value
    return result


def _document(raw):
    document = (
        {} if raw is None else json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object)
    )
    if not isinstance(document, dict) or not isinstance(document.get('companies', {}), dict):
        raise ValueError('expected a financials object with a companies mapping')
    for company in document.get('companies', {}).values():
        if not isinstance(company, dict):
            raise ValueError('expected company records to be objects')
        if any(
            company.get(field) is not None and not isinstance(company[field], str)
            for field in ('source', 'company', 'last_checked')
        ):
            raise ValueError('invalid financials company metadata')
        snapshots = company.get('snapshots', [])
        if not isinstance(snapshots, list) or any(
            not isinstance(snapshot, dict)
            or (
                snapshot.get('recorded_at') is not None
                and not isinstance(snapshot['recorded_at'], str)
            )
            for snapshot in snapshots
        ):
            raise ValueError('expected snapshots to be a list of snapshot objects')
    return document


def _same(left, right):
    if left is _MISSING or right is _MISSING:
        return left is right
    return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)


def _merge(base, current, intended, section, conflicts):
    result = {}
    for key in dict.fromkeys((*intended, *current, *base)):
        old, edited, new = (item.get(key, _MISSING) for item in (base, current, intended))
        if _same(edited, old):
            value = new
        elif _same(new, old) or _same(edited, new):
            value = edited
        else:
            conflicts.append(f'{section}.{key}')
            continue
        if value is not _MISSING:
            result[key] = value
    return result


def merge_financial_exports(path, baseline, current, intended):
    try:
        base, edited, new = (_document(raw) for raw in (baseline, current, intended))
    except ValueError as error:
        raise ExportConflict(f'{path}: {error}') from error

    conflicts: list[str] = []
    if not _same(base.get('schema_version', _MISSING), edited.get('schema_version', _MISSING)):
        conflicts.append('metadata.schema_version')
    companies = _merge(
        base.get('companies', {}),
        edited.get('companies', {}),
        new.get('companies', {}),
        'companies',
        conflicts,
    )
    # Merge a presence marker with the metadata so the existing key order is kept.
    old_meta, edited_meta, new_meta = (
        {key: (None if key == 'companies' else value) for key, value in item.items()}
        for item in (base, edited, new)
    )
    metadata = _merge(old_meta, edited_meta, new_meta, 'metadata', conflicts)
    if conflicts:
        raise ExportConflict(f'{path}: conflicting edits to {", ".join(dict.fromkeys(conflicts))}')
    if companies or 'companies' in metadata:
        metadata['companies'] = companies
    return metadata
