"""Expected Applicant schema definitions by migration version."""

import sqlite3
from contextlib import closing
from functools import lru_cache


def objects(db: sqlite3.Connection) -> dict[tuple[str, str], str | None]:
    return {
        (kind, name): definition
        for kind, name, definition in db.execute(
            "SELECT type, name, sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
        )
    }


@lru_cache(maxsize=32)
def expected_schema(migrations: tuple[tuple[str, ...], ...]):
    # Build only through the requested version. A later failing step must not
    # prevent an earlier migration from committing.
    with closing(sqlite3.connect(':memory:')) as db:
        for migration in migrations:
            for statement in migration:
                db.execute(statement)
        return objects(db)


def incompatible_object(actual, expected) -> str | None:
    return next(
        (name for (kind, name), sql in expected.items() if actual.get((kind, name)) != sql),
        None,
    )
