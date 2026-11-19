"""Exercise language imports, immutable ranking, and database CLI boundaries."""

import io
import sqlite3
from contextlib import closing, redirect_stderr, redirect_stdout
from copy import deepcopy
from unittest.mock import patch

import pytest

from applicant.cli import main
from applicant.domain.language_extraction import extract_language
from applicant.domain.language_fit import evaluate_language, preferred_score
from applicant.domain.ranking_v2 import CandidateProfileV3, RankingRunV4
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
