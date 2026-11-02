"""Strict preference JSON reads and atomic exports."""

import json
from decimal import Decimal
from pathlib import Path

from ..domain.canonical import canonical_json
from ..domain.survey import PreferenceDocument
from ..errors import ConfigError, StoreError
from ..files import write_document


def _object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError(f'duplicate JSON key: {name}')
        result[name] = value
    return result


def _constant(value: str) -> None:
    raise ValueError(f'nonfinite JSON number: {value}')


def read_preferences(path: str | Path) -> PreferenceDocument:
    try:
        text = Path(path).read_text(encoding='utf-8')
    except OSError as error:
        raise StoreError(f'could not read preferences {path}: {error}') from error
    except UnicodeError as error:
        raise ConfigError(f'invalid preferences {path}: {error}') from error
    try:
        data = json.loads(
            text, parse_float=Decimal, parse_constant=_constant, object_pairs_hook=_object
        )
        return PreferenceDocument.model_validate(data)
    except ValueError as error:
        raise ConfigError(f'invalid preferences {path}: {error}') from error


def export_preferences(path: str | Path, document: PreferenceDocument, *, database: Path) -> None:
    target = Path(path).resolve()
    if target in {
        database.resolve(),
        *(Path(str(database) + s).resolve() for s in ('-wal', '-shm')),
    }:
        raise ConfigError('export must not replace the database or its WAL/SHM sidecars')
    document = PreferenceDocument.model_validate_json(canonical_json(document))
    data = document.model_dump() | {'preferences': document.expanded()}
    try:
        write_document(path, json.loads(canonical_json(data)), trailing_newline=True)
    except OSError as error:
        raise StoreError(f'could not export preferences {path}: {error}') from error
