"""Ranking repositories share a caller-owned connection and transaction."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TypeVar

from pydantic import ValidationError

from ...domain.canonical import canonical_json
from ...domain.contracts import Contract
from ...errors import StoreError

T = TypeVar('T', bound=Contract)


def validated(value: T) -> T:
    # Capture caller-owned nested objects and reject invalid model_copy updates.
    return type(value).model_validate_json(canonical_json(value))


def decode(model: type[T], payload: str) -> T:
    try:
        return model.model_validate_json(payload)
    except ValidationError as error:
        raise StoreError(f'invalid historical {model.__name__}: {error}') from error


@contextmanager
def write_transaction(db: sqlite3.Connection) -> Iterator[None]:
    if db.in_transaction:
        raise StoreError('ranking service requires a connection without an active transaction')
    try:
        db.execute('BEGIN IMMEDIATE')
        yield
        db.commit()
    except BaseException as error:
        db.rollback()
        if isinstance(error, sqlite3.Error):
            raise StoreError(f'ranking transaction failed: {error}') from error
        raise


def require_transaction(db: sqlite3.Connection) -> None:
    if not db.in_transaction:
        raise StoreError('repository writes require a caller-owned transaction')
