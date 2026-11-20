"""Check migration boundaries with independent SQLite connections."""

import os
import sqlite3
from contextlib import closing
from unittest.mock import patch

import pytest

from applicant.cli import main
from applicant.cli.database_preflight import inspect, preflight
from applicant.errors import StoreError
from applicant.infra.store.database_schema import expected_schema, objects
from applicant.infra.store.sqlite import MIGRATIONS, Store


def seed(path, version):
    with (
        patch('applicant.infra.store.sqlite.MIGRATIONS', MIGRATIONS[:version]),
        Store(path) as store,
    ):
        store.db.execute("INSERT INTO documents (name, digest) VALUES ('keep', 'value')")
        store.db.commit()


def check(path, version):
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('PRAGMA user_version').fetchone() == (version,)
        assert objects(db) == expected_schema(MIGRATIONS[:version])
        assert db.execute("SELECT digest FROM documents WHERE name='keep'").fetchone() == ('value',)


@pytest.mark.parametrize('failure', ['ddl', 'before commit', 'after commit'])
def test_resume_after_failed_or_interrupted_step(tmp_path, capsys, failure):
    path = tmp_path / 'history.db'
    seed(path, 12)
    connect = sqlite3.connect
    traced = []

    class Connection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            result = super().execute(sql, parameters)
            if failure == 'ddl' and sql == MIGRATIONS[13][0]:
                raise sqlite3.OperationalError('injected step failure')
            return result

        def commit(self):
            version = self.execute('PRAGMA user_version').fetchone()[0]
            if version == 14 and self.in_transaction and failure == 'before commit':
                raise KeyboardInterrupt
            super().commit()
            if version == 14 and failure == 'after commit':
                raise KeyboardInterrupt

    def instrument(database, *args, **kwargs):
        if database == path:
            kwargs['factory'] = Connection
        return connect(database, *args, **kwargs)

    args = ['--no-log-file', 'db', 'upgrade', '--database', str(path)]
    with patch('sqlite3.connect', side_effect=instrument):
        assert main(args) == (1 if failure == 'ddl' else 130)
    version = 14 if failure == 'after commit' else 13
    assert str(version) in capsys.readouterr().err
    check(path, version)

    def trace(database, *args, **kwargs):
        db = connect(database, *args, **kwargs)
        if database == path:
            db.set_trace_callback(traced.append)
        return db

    with patch('sqlite3.connect', side_effect=trace):
        assert main(args) == 0
    assert not any(statement in traced for statement in MIGRATIONS[12])
    if version == 13:
        assert all(statement in traced for statement in MIGRATIONS[13])
    check(path, 14)


@pytest.mark.parametrize('race', ['drop', 'upgrade'])
def test_recheck_after_prompt(tmp_path, race):
    path = tmp_path / 'history.db'
    seed(path, 13)

    def accept(_prompt):
        if race == 'drop':
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('DROP INDEX jobs_order')
        else:
            with Store(path):
                pass
        return 'yes'

    with (
        patch.dict(os.environ, {'CI': 'false'}),
        patch('sys.stdin.isatty', return_value=True),
        patch('sys.stderr.isatty', return_value=True),
        patch('applicant.interaction.Terminal.ask', side_effect=accept),
    ):
        if race == 'drop':
            with pytest.raises(StoreError, match='jobs_order'):
                preflight((path,), True)
            assert inspect_version(path) == 13
            with closing(sqlite3.connect(path)) as db:
                assert ('table', 'job_language_assertions') not in objects(db)
        else:
            preflight((path,), True)
            assert inspect(path) == 14


def inspect_version(path):
    with closing(sqlite3.connect(path)) as db:
        return db.execute('PRAGMA user_version').fetchone()[0]
