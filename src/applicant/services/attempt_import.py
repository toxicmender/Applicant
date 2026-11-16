"""Stable snapshots and deterministic merging of legacy application journals."""

import json
from collections import defaultdict

from ..domain.canonical import content_hash
from ..errors import ConfigError
from ..financials.tracker import _utc_timestamp
from ..infra.path_identity import physical_path
from ..infra.store.financials_lock import export_lock

_ROUTING = ('destination', 'log_path', 'source_path')
_STATES = {
    'armed',
    'uncertain',
    'submitted',
    'not-submitted',
    'abandoned',
    'retried',
    'failed-before-dispatch',
}


def _load(path):
    with export_lock(path):
        before = path.stat()
        raw = path.read_bytes()
        if path.read_bytes() != raw:
            raise ConfigError(f'{path}: changed during import; retry with a completed file')
        after = path.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ConfigError(f'{path}: changed during import; retry with a completed file')
    try:
        value = json.loads(raw)
    except ValueError as error:
        raise ConfigError(f'{path}: invalid legacy application journal') from error
    if not isinstance(value, dict) or 'schema_version' in value:
        raise ConfigError(f'{path}: expected a legacy application journal')
    for identifier, entry in value.items():
        if (
            not isinstance(entry, dict)
            or entry.get('state') not in _STATES
            or not isinstance(entry.get('account'), str)
        ):
            raise ConfigError(f'{path}: invalid legacy attempt {identifier}')
    return value


def snapshots(paths):
    selected = set()
    for name in paths:
        path = physical_path(name)
        if path.is_dir():
            for child in path.iterdir():
                if not child.is_file() or child.name.startswith(('.', '~')):
                    continue
                if child.name.endswith('.attempts.json'):
                    selected.add(child)
                elif child.suffix.lower() == '.csv':
                    journal = child.with_name(f'.{child.name}.attempts.json')
                    if journal.is_file():
                        selected.add(journal)
        elif path.suffix.lower() == '.csv':
            selected.add(path.with_name(f'.{path.name}.attempts.json'))
        else:
            selected.add(path)
    return [(path, _load(path)) for path in sorted(selected, key=str)]


def canonical_entry(entry):
    core = {
        field: value
        for field, value in entry.items()
        if field not in _ROUTING and value is not None
    }
    for field in ('recorded_at', 'resolved_at'):
        instant = _utc_timestamp(core.get(field))
        if instant is not None:
            core[field] = instant.isoformat()
    return core


def _combine(first, second):
    merged = dict(first)
    for field, value in second.items():
        if field in merged and isinstance(merged[field], dict) and isinstance(value, dict):
            child = _combine(merged[field], value)
            if child is None:
                return None
            merged[field] = child
            continue
        if merged.get(field) is not None and merged[field] != value:
            return None
        merged[field] = value
    return merged


def merge(document, candidates):
    groups = defaultdict(dict)
    audit = document.setdefault('resolved_conflicts', [])
    handled = {(item['attempt'], item['digest']) for item in audit}
    entries = document['entries']
    conflicts = document.setdefault('conflicts', {})
    for path, batch in candidates:
        for identifier, entry in batch.items():
            digest = content_hash(canonical_entry(entry))
            if (identifier, digest) in handled:
                continue
            candidate = groups[identifier].setdefault(digest, {'entry': entry, 'sources': []})
            candidate['sources'].append(str(path))
    for identifier, group in sorted(groups.items()):
        original = entries.get(identifier)
        merged = canonical_entry(original) if original is not None else {}
        for candidate in group.values():
            merged = _combine(merged, canonical_entry(candidate['entry']))
            if merged is None:
                break
        if merged is not None and identifier not in conflicts:
            routing = original if original is not None else group[min(group)]['entry']
            entries[identifier] = {
                **merged,
                **{field: routing[field] for field in _ROUTING if field in routing},
            }
            for digest in group:
                audit.append({'attempt': identifier, 'digest': digest, 'decision': 'merged'})
            continue
        conflict = conflicts.setdefault(identifier, {})
        for digest, candidate in group.items():
            previous = conflict.get(digest, {}).get('sources', [])
            candidate['sources'] = sorted(set(previous + candidate['sources']))
            conflict[digest] = candidate
