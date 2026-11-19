"""Exercise language imports, immutable ranking, and database CLI boundaries."""

import io
import json
import os
import sqlite3
import subprocess
from contextlib import closing, redirect_stderr, redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest

from applicant.cli import main
from applicant.cli.database_preflight import pin_paths
from applicant.domain.language_extraction import extract_language
from applicant.domain.language_fit import evaluate_language, preferred_score
from applicant.domain.ranking_v2 import CandidateProfileV3, JobRankingV3, RankingRunV4
from applicant.errors import StoreError
from applicant.infra.store.database_paths import PINNED, sqlite_path
from applicant.infra.store.sqlite import MIGRATIONS, Store
from applicant.services.research import export_job_versions, import_research, template
from tests import test_ranking_cli


def test_old_database_rejected_before_search(tmp_path):
    database = tmp_path / 'applicant.db'
    with closing(sqlite3.connect(database)) as db, db:
        for migration in MIGRATIONS[:8]:
            for statement in migration:
                db.execute(statement)
        db.execute('PRAGMA user_version=8')
    error = io.StringIO()
    with (
        redirect_stderr(error),
        patch('applicant.search.Jobs', side_effect=AssertionError('provider called')),
    ):
        code = main(['--data-dir', str(tmp_path), '--no-log-file', 'search', 'engineer'])
    assert code == 1
    assert 'db upgrade --database' in error.getvalue()
    with closing(sqlite3.connect(database)) as db:
        assert db.execute('PRAGMA user_version').fetchone() == (8,)
    with redirect_stdout(io.StringIO()) as output:
        for _ in range(2):
            assert (
                main(
                    [
                        '--data-dir',
                        str(tmp_path),
                        '--no-log-file',
                        'db',
                        'upgrade',
                        '--database',
                        'applicant.db',
                    ]
                )
                == 0
            )
    assert 'already current at schema 14' in output.getvalue()


def test_template_to_archived_language_run():
    fixture = test_ranking_cli.RankingCliTest()
    fixture.setUp()
    try:
        bundle = template('language')
        # The orchestration fixture uses a fixed historical calculation time.
        stamp = '2020-01-01T00:00:00+00:00'
        bundle['companies'][0]['recorded_at'] = stamp
        bundle['observations'][0].update(acquired_at=stamp, observed_at=stamp)
        gathered = bundle['evidence'][0]
        gathered['acquired_at'] = stamp
        gathered['excerpt'] = 'English B2 required'
        dry_run = import_research(fixture.database, bundle, dry_run=True)
        imported = import_research(fixture.database, bundle)
        assert dry_run['resolved_members'] == imported['resolved_members']
        assert dry_run['resolved_members'][0]['member_id'] == gathered['payload']['member_id']
        exported = export_job_versions(fixture.database, {'job_version_ids': ['example-job-v1']})
        run = fixture.service(versions_path=fixture.bundle(exported.jobs))
        assert isinstance(run, RankingRunV4)
        groups = run.results[0].language_groups
        assert groups[0]['status'] == 'unknown'
        assert groups[0]['members'][0]['reason'] == 'skill_missing'
        code = main(
            [
                '--data-dir',
                str(fixture.root),
                '--no-log-file',
                'rank',
                'replay',
                run.ranking_run_id,
                '--provenance',
                'provenance.json',
            ]
        )
        assert code == 0
        with Store.open_readonly(fixture.database) as store:
            assert store.db.execute('SELECT count(*) FROM job_language_assertions').fetchone() == (
                1,
            )
    finally:
        fixture.doCleanups()


def test_explicit_language_extraction_only():
    assert not extract_language('Python required. Diese Stelle ist ausgeschrieben.')
    spoken, written = extract_language('Spoken and written German B2 required.')
    assert spoken['payload']['threshold']['level'] == 'B2'
    assert {spoken['payload']['skill'], written['payload']['skill']} == {'spoken', 'written'}
    assert extract_language('German not required.')[0]['payload']['strength'] == 'not_required'
    required, qualified = extract_language('German required; German fluency not required.')
    assert required['payload']['strength'] == 'required'
    assert required['payload']['threshold']['kind'] == 'no_minimum'
    assert qualified['payload']['strength'] == 'not_required'


def test_language_strength_and_thresholds_follow_clause_boundaries():
    assert not extract_language('Python is required, and our documentation is available in German.')
    assert not extract_language('German documentation is available and Python is required.')
    text = (
        'German is a plus (not required).\n'
        '• Working knowledge of Dutch helpful.\n'
        '• No prior French required.\n'
        'We use German internally; English required for the role.'
    )
    facts = [item['payload'] for item in extract_language(text)]
    assert [(fact['language'], fact['strength']) for fact in facts] == [
        ('de', 'preferred'),
        ('nl', 'preferred'),
        ('fr', 'not_required'),
        ('en', 'required'),
    ]
    assert not any(fact['interpretation_review'] for fact in facts)
    english, french, german = [
        item['payload']
        for item in extract_language('English or French required, and German also required.')
    ]
    assert english['expression'] == french['expression']
    assert english['expression']['operator'] == 'any_of'
    assert german['expression'] is None and german['clause_id'] != english['clause_id']
    separate = extract_language('Spoken B2 and written C1 German required.')
    assert {
        item['payload']['skill']: item['payload']['threshold']['level'] for item in separate
    } == {
        'spoken': 'B2',
        'written': 'C1',
    }


def test_language_alternatives_have_independent_skills_and_thresholds():
    alternatives = [
        item['payload']
        for item in extract_language('Spoken German B2 or written French C1 required.')
    ]
    assert [(p['language'], p['skill'], p['threshold']['level']) for p in alternatives] == [
        ('de', 'spoken', 'B2'),
        ('fr', 'written', 'C1'),
    ]
    assert alternatives[0]['expression']['operator'] == 'any_of'
    assert not any(p['interpretation_review'] for p in alternatives)


def test_hard_exclusion_preserves_language_review_in_cli_and_replay():
    fixture = test_ranking_cli.RankingCliTest()
    fixture.setUp()
    try:
        jobs = fixture.mixed_jobs()
        hedge = 'Fluent German preferred, though client-facing roles may require it.'
        optional = 'German B1 or B2 preferred.'
        assert extract_language(hedge)[0]['payload']['strength'] == 'required'
        assert extract_language(optional)[0]['payload']['strength'] == 'preferred'
        jobs = tuple(
            job.model_copy(
                update={
                    'job_data': job.job_data
                    | {
                        'url': f'https://example.com/jobs/{job.job_version_id}',
                        'language_clauses': extract_language(text),
                    }
                }
            )
            for job, text in zip(jobs, (optional, hedge, hedge), strict=True)
        )
        run = fixture.service(versions_path=fixture.bundle(jobs))
        assert isinstance(run, RankingRunV4)
        results = {
            result.eligibility: result for result in run.results if isinstance(result, JobRankingV3)
        }
        assert len(results) == len(run.results)
        excluded = results['excluded']
        assert excluded.overall_score is excluded.confidence is excluded.coverage is None
        assert all(d.status == 'not_evaluated' for d in excluded.dimensions)
        assert any(c.status == 'fail' for c in excluded.decision.criteria)
        assert excluded.language_groups[0]['members'][0]['reason'] == 'interpretation_review'
        assert results['review_required'].language_groups[0]['strength'] == 'required'
        assert preferred_score(results['eligible'].language_groups)[0] is None

        def cli(*args):
            with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
                code = main(['--no-log-file', '--data-dir', str(fixture.root), 'rank', *args])
            assert code == 0, err.getvalue()
            return out.getvalue()

        shown = cli('show', run.ranking_run_id)
        assert 'Eligibility: excluded' in shown and 'Needs review:' in shown
        groups = json.loads(cli('show', run.ranking_run_id, '--json'))['groups']
        assert [row['job_id'] for row in groups['excluded']['results']] == [excluded.job_id]
        assert all(
            row['job_id'] != excluded.job_id
            for state in ('eligible', 'review_required')
            for row in groups[state]['results']
        )
        explanation = cli('explain', run.ranking_run_id, excluded.job_id, '--json')
        assert 'interpretation_review' in explanation
        assert (
            next(c.reason_code for c in excluded.decision.criteria if c.status == 'fail')
            in explanation
        )
        assert 'matches' in cli('replay', run.ranking_run_id, '--provenance', 'provenance.json')
    finally:
        fixture.doCleanups()


def test_sidecars_at_chosen_name_and_late_alternate_name(tmp_path):
    chosen, alias = tmp_path / 'canonical.db', tmp_path / 'alternate.db'
    chosen.touch()
    os.link(chosen, alias)
    wal = chosen.with_name(chosen.name + '-wal')
    wal.touch()
    chosen.with_name(chosen.name + '-shm').touch()
    token = PINNED.set(pin_paths((chosen, alias)))
    try:
        assert sqlite_path(chosen) == chosen
        assert sqlite_path(alias) == chosen
        journal = alias.with_name(alias.name + '-journal')
        journal.touch()
        with pytest.raises(StoreError, match='recovery sidecars appeared'):
            sqlite_path(alias)
        assert journal.exists() and wal.exists()
    finally:
        PINNED.reset(token)
    with pytest.raises(StoreError, match='sidecars at multiple aliases'):
        pin_paths((chosen, alias))


def upgrade_cli(path):
    return main(['--no-log-file', 'db', 'upgrade', '--database', str(path)])


def test_file_symlink_sidecars_are_preserved(tmp_path, capsys):
    target = tmp_path / 'target.db'
    alias = tmp_path / 'alias.db'
    with Store(target):
        pass
    try:
        alias.symlink_to(target)
    except OSError as error:
        if os.name != 'nt' or getattr(error, 'winerror', None) not in {5, 1314}:
            raise
        pytest.skip(f'Windows file symlinks unavailable: {error}')
    alias_wal = alias.with_name('alias.db-wal')
    target_shm = target.with_name('target.db-shm')
    for paths in ((alias_wal,), (alias_wal, target_shm)):
        before = {}
        for path in paths:
            path.write_bytes(b'sidecar recovery data')
            info = path.stat()
            before[path] = info.st_dev, info.st_ino, info.st_mtime_ns, path.read_bytes()
        assert upgrade_cli(alias) == 1
        error = capsys.readouterr().err
        assert str(target) in error
        for path in paths:
            info = path.stat()
            assert str(path) in error
            assert (info.st_dev, info.st_ino, info.st_mtime_ns, path.read_bytes()) == before[path]
    alias_wal.unlink()
    target_shm.unlink()
    with Store(target) as writer:
        writer.db.execute('PRAGMA wal_autocheckpoint = 0')
        writer.db.execute('PRAGMA user_version = 15')
        assert upgrade_cli(alias) == 1
        assert 'schema 15 requires a newer Applicant' in capsys.readouterr().err


def test_retargeted_directory_alias(tmp_path):
    old, new, alias = (tmp_path / name for name in ('old', 'new', 'alias'))
    old.mkdir()
    new.mkdir()

    def link(target):
        if os.name == 'nt':
            subprocess.run(['cmd', '/c', 'mklink', '/J', str(alias), str(target)], check=True)
        else:
            alias.symlink_to(target, target_is_directory=True)

    link(old)
    try:
        supplied = alias / 'target.db'
        token = PINNED.set(pin_paths((supplied,)))
        try:
            alias.rmdir() if os.name == 'nt' else alias.unlink()
            link(new)
            with pytest.raises(StoreError, match='target_changed'):
                Store(supplied)
            assert not (old / 'target.db').exists()
            assert not (new / 'target.db').exists()
        finally:
            PINNED.reset(token)
    finally:
        alias.rmdir() if os.name == 'nt' else alias.unlink()


@pytest.mark.skipif(os.name != 'nt', reason='Windows short paths')
def test_windows_short_directory_has_one_sidecar_location(tmp_path):
    if os.name != 'nt':
        return
    import ctypes

    long = tmp_path / 'directory with long name'
    long.mkdir()
    buffer = ctypes.create_unicode_buffer(32768)
    size = ctypes.WinDLL('kernel32').GetShortPathNameW(str(long), buffer, len(buffer))
    if not size or Path(buffer.value) == long:
        pytest.skip('filesystem has no 8.3 alias')
    short = Path(buffer.value)
    supplied = short / 'missing' / 'test.db'
    token = PINNED.set(pin_paths((supplied,)))
    try:
        with Store(supplied) as writer:
            assert writer.path == long / 'missing' / 'test.db'
            assert upgrade_cli(supplied) == 0
    finally:
        PINNED.reset(token)


def test_retirement_never_revives_a_predecessor(tmp_path):
    database = tmp_path / 'applicant.db'
    bundle = template('language')
    original = bundle['evidence'][0]
    later = deepcopy(original)
    later['evidence_id'] = 'later'
    later['payload'].update(supersedes=['example-evidence'], threshold={'kind': 'no_minimum'})
    retirement = deepcopy(original)
    retirement['evidence_id'] = 'retired'
    retirement['payload'].update(action='retire', supersedes=['later'])
    # Reverse revision order to exercise endpoint validation after the whole batch is inserted.
    bundle['evidence'] = [retirement, later, original]
    import_research(database, bundle)
    job = export_job_versions(database, {'job_version_ids': ['example-job-v1']}).jobs[0]
    assert job.evidence[0].payload['assertions'] == []
    ambiguous = deepcopy(original)
    ambiguous['evidence_id'] = 'new-unlocated'
    del ambiguous['payload']['member_id']
    with pytest.raises(ValueError, match='explicit member ID'):
        import_research(database, {'evidence': [ambiguous]})
    with Store.open_readonly(database) as store:
        assert store.db.execute('SELECT count(*) FROM job_language_assertions').fetchone() == (3,)


def test_cross_scale_and_skill_are_unknown_and_preferred_conflict_is_counted(tmp_path):
    database = tmp_path / 'applicant.db'
    bundle = template('language')
    fact = bundle['evidence'][0]['payload']
    fact['skill'] = 'spoken'
    import_research(database, bundle)
    job = export_job_versions(database, {'job_version_ids': ['example-job-v1']}).jobs[0]
    profile = CandidateProfileV3.model_validate(
        {
            'profile_id': 'test',
            'experience_years': 3,
            'primary_discipline': 'Software',
            'languages': [
                {
                    'language': 'en',
                    'overall': {
                        'level': {'kind': 'level', 'scale': 'cefr', 'level': 'B2'},
                    },
                },
            ],
        }
    )
    expected = evaluate_language(job, profile)[0]['members'][0]
    assert (expected['status'], expected['reason']) == ('unknown', 'skill_missing')
    candidate = profile.model_dump()
    candidate['languages'][0]['spoken'] = {
        'level': {'kind': 'level', 'scale': 'named', 'level': 'fluent'},
    }
    profile = CandidateProfileV3.model_validate(candidate)
    compared = evaluate_language(job, profile)[0]['members'][0]
    assert (compared['status'], compared['reason']) == ('unknown', 'scale_incomparable')
    score, resolved_coverage = preferred_score(
        [
            {'strength': 'preferred', 'status': 'pass'},
            {'strength': 'preferred', 'status': 'conflict'},
            {'strength': 'required', 'status': 'conflict'},
        ]
    )
    assert score == 50
    assert resolved_coverage == 0.5
