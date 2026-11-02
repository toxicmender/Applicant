"""Strict ranking JSON inputs with Decimal config and lossless raw JSON shapes."""

import json
import math
from decimal import Decimal
from pathlib import Path

from ..errors import ConfigError, StoreError


def _object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON key: {key}')
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ValueError(f'nonfinite JSON number: {value}')


def _float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f'nonfinite JSON number: {value}')
    return number


def read_ranking_document(path: Path, *, decimal: bool = False):
    try:
        text = path.read_text(encoding='utf-8')
    except OSError as error:
        raise StoreError(f'could not read ranking input {path}: {error}') from error
    except UnicodeError as error:
        raise ConfigError(f'invalid ranking input {path}: {error}') from error
    try:
        return json.loads(
            text,
            object_pairs_hook=_object,
            parse_constant=_constant,
            parse_float=Decimal if decimal else _float,
        )
    except ValueError as error:
        raise ConfigError(f'invalid ranking input {path}: {error}') from error
