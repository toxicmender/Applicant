"""Revision-safe editable documents and noninteractive survey journeys."""

import io
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal, localcontext
from pathlib import Path
from unittest import mock

from applicant.cli import main
from applicant.domain.canonical import canonical_json
from applicant.domain.survey import PreferenceDocument
from applicant.errors import ConfigError, StoreError
from applicant.infra.preference_documents import export_preferences, read_preferences
from applicant.infra.store.profiles import ProfileNotFound, RevisionConflict
from applicant.infra.store.sqlite import Store
from applicant.services.preferences import save_profile
from applicant.services.survey import editable_profile, import_preferences, load_profile
from tests.ranking_fixtures import NOW
from tests.test_ranking_contracts import profile

FIXTURE = Path(__file__).parent / 'fixtures' / 'ranking' / 'preferences.json'


class SurveyTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.database = self.root / 'applicant.db'
        self.path = self.root / 'preferences.json'
        self.path.write_bytes(FIXTURE.read_bytes())

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with (
            redirect_stdout(out),
            redirect_stderr(err),
            mock.patch('applicant.cli.survey.now', return_value=NOW),
        ):
            code = main(['--no-log-file', '--data-dir', str(self.root), *args])
        return code, out.getvalue(), err.getvalue()

    def test_document_golden_precision_and_context_independence(self):
        document = read_preferences(self.path)
        with localcontext() as context:
            context.prec = 2
            parsed = PreferenceDocument.model_validate_json(canonical_json(document))
            candidate = parsed.to_profile(updated_at=NOW)
        self.assertEqual(candidate.schema_version, 2)
        self.assertEqual(candidate.experience_years, Decimal('5.5'))
        self.assertEqual(candidate.work_country, 'IN')
        self.assertNotIn('updated_at', parsed.expanded())
        self.path.write_text(
            FIXTURE.read_text().replace('"5.5"', '5.12345678901234567890123456789'),
            encoding='utf-8',
        )
        self.assertEqual(
            read_preferences(self.path).preferences['experience_years'],
            Decimal('5.12345678901234567890123456789'),
        )

    def test_invalid_documents_fail_without_creating_database(self):
        original = json.loads(FIXTURE.read_text())
        bad = [
            '{}',
            '[]',
            '{',
            '{"profile_id":"x","profile_id":"y"}',
            json.dumps(original | {'schema_version': 9}),
            json.dumps(original | {'expected_revision': True}),
            json.dumps(original | {'expected_revision': -1}),
            json.dumps(original | {'extra': 1}),
        ]
        for changes in (
            {'schema_version': 2},
            {'profile_id': 'override'},
            {'updated_at': 'today'},
            {'minimum_base_salary': 100000},
            {'work_country': 'XX'},
            {'weights': {'compensation': 1}},
            {'experience_years': float('nan')},
            {'preferred_base_salary': float('inf')},
        ):
            bad.append(json.dumps(original | {'preferences': original['preferences'] | changes}))
        for text in bad:
            with self.subTest(text=text):
                self.path.write_text(text, encoding='utf-8')
                code, out, err = self.cli('survey', '--from-file', 'preferences.json')
                self.assertEqual(code, 2)
                self.assertEqual(out, '')
                self.assertIn('invalid preferences', err)
                self.assertFalse(self.database.exists())

    def test_validate_and_missing_store_dry_run_do_not_write(self):
        self.assertEqual(self.cli('survey', '--validate', 'preferences.json')[0], 0)
        code, out, _ = self.cli('survey', '--dry-run', '--from-file', 'preferences.json')
        self.assertEqual(code, 0)
        self.assertIn('proposed profile default revision 1', out)
        self.assertFalse(self.database.exists())
        data = json.loads(self.path.read_text()) | {'expected_revision': 1}
        self.path.write_text(json.dumps(data), encoding='utf-8')
        self.assertEqual(self.cli('survey', '--dry-run', '--from-file', 'preferences.json')[0], 2)
        self.assertFalse(self.database.exists())

    def test_import_export_edit_stale_revision_and_historical_reads(self):
        self.assertEqual(self.cli('survey', '--from-file', 'preferences.json')[0], 0)
        original = self.database.read_bytes()
        code, out, _ = self.cli('survey', '--show', '--json')
        self.assertEqual(code, 0)
        archived = json.loads(out)
        self.assertEqual(archived['profile_revision'], 1)
        self.assertEqual(archived['updated_at'], '2026-11-02T00:00:00.000000Z')
        self.assertEqual(self.cli('survey', '--export', 'export.json')[0], 0)
        exported = read_preferences(self.root / 'export.json')
        self.assertEqual(exported.expected_revision, 1)
        self.assertEqual(self.cli('survey', '--dry-run', '--from-file', 'export.json')[0], 0)
        self.assertEqual(self.database.read_bytes(), original)
        self.assertEqual(self.cli('survey', '--from-file', 'export.json')[0], 0)
        self.assertEqual(self.cli('survey', '--from-file', 'export.json')[0], 2)
        self.assertEqual(self.cli('survey', '--dry-run', '--from-file', 'export.json')[0], 2)
        self.assertEqual(load_profile(self.database, 'default').profile_revision, 2)
        self.assertEqual(
            json.loads(self.cli('survey', '--show', '--revision', '1', '--json')[1]), archived
        )
        code, out, _ = self.cli('survey', '--show')
        self.assertEqual(code, 0)
        self.assertIn('annual base pay', out)
        self.assertIn('currently do not add ranking signals', out)
        self.assertEqual(self.cli('survey', '--show', '--revision', '9')[0], 4)
        self.assertEqual(self.cli('survey', '--show', '--profile', 'unknown')[0], 4)

    def test_missing_profiles_and_wrong_options_create_nothing(self):
        self.assertEqual(self.cli('survey', '--show')[0], 4)
        self.assertEqual(self.cli('survey', '--export', 'export.json')[0], 4)
        for options in (
            [],
            ['--store', 'files', '--validate', 'preferences.json'],
            ['--dry-run'],
            ['--revision', '1'],
            ['--json'],
            ['--show', '--profile', ' '],
            ['--profile', 'x', '--from-file', 'preferences.json'],
            ['--profile', 'x', '--validate', 'preferences.json'],
        ):
            with self.subTest(options=options):
                self.assertEqual(self.cli('survey', *options)[0], 2)
        self.assertFalse(self.database.exists())
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main(['survey', '--show', '--validate', 'x'])
        self.assertEqual(error.exception.code, 2)

    def test_explicit_database_and_configured_listing_default(self):
        self.assertEqual(
            self.cli(
                '--store',
                'sqlite',
                'survey',
                '--database',
                'custom/db.sqlite',
                '--from-file',
                'preferences.json',
            )[0],
            0,
        )
        self.assertFalse(self.database.exists())
        self.assertEqual(self.cli('survey', '--database', 'custom/db.sqlite', '--show')[0], 0)
        config = self.root / 'applicant.toml'
        config.write_text('[files]\nlisting = "elsewhere/jobs.json"\n', encoding='utf-8')
        code, out, _ = self.cli(
            'survey', '--config', str(config), '--from-file', 'preferences.json'
        )
        self.assertEqual(code, 0)
        self.assertIn(str(self.root / 'elsewhere' / 'applicant.db'), out)
        self.assertFalse(self.database.exists())

    def test_legacy_profile_exports_and_imports_as_new_v2_revision(self):
        with Store(self.database) as store:
            saved = save_profile(store, profile(), expected_revision=0)
        doc = editable_profile(saved)
        self.assertIsNone(doc.expanded()['work_country'])
        result = import_preferences(self.database, doc, updated_at=NOW)
        self.assertEqual((result.schema_version, result.profile_revision), (2, 2))
        self.assertEqual(load_profile(self.database, 'candidate', 1), saved)
        with self.assertRaises(RevisionConflict):
            import_preferences(self.database, doc, updated_at=NOW)

    def test_export_validation_protected_targets_and_atomic_failure(self):
        doc = read_preferences(self.path)
        for suffix in ('', '-wal', '-shm'):
            with self.subTest(suffix=suffix), self.assertRaises(ConfigError):
                export_preferences(Path(str(self.database) + suffix), doc, database=self.database)
        output = self.root / 'out.json'
        output.write_text('old', encoding='utf-8')
        with (
            mock.patch('applicant.files.os.replace', side_effect=PermissionError('locked')),
            self.assertRaises(StoreError),
        ):
            export_preferences(output, doc, database=self.database)
        self.assertEqual(output.read_text(), 'old')
        self.assertEqual(list(self.root.glob('.out.json.*.tmp')), [])
        doc.preferences['updated_at'] = 'bad'
        with self.assertRaises(ValueError):
            import_preferences(self.database, doc, updated_at=NOW)
        self.assertFalse(self.database.exists())

    def test_io_failures_remain_store_errors_and_do_not_become_absence(self):
        self.path.write_bytes(b'\xff')
        with self.assertRaises(ConfigError):
            read_preferences(self.path)
        with self.assertRaises(StoreError):
            read_preferences(self.root / 'missing.json')
        with (
            mock.patch.object(Path, 'stat', side_effect=PermissionError('denied')),
            self.assertRaises(StoreError),
        ):
            load_profile(self.database, 'default')
        self.assertEqual(self.cli('survey', '--validate', 'missing.json')[0], 1)
        with self.assertRaises(ProfileNotFound):
            load_profile(self.database, 'default')

    def test_two_importers_can_publish_only_one_next_revision(self):
        saved = import_preferences(self.database, read_preferences(self.path), updated_at=NOW)
        document = editable_profile(saved)
        barrier = threading.Barrier(2)

        def save():
            barrier.wait(timeout=5)
            try:
                return import_preferences(self.database, document, updated_at=NOW).profile_revision
            except RevisionConflict:
                return 'conflict'

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(save) for _ in range(2)]
            results = [future.result(timeout=15) for future in futures]
        self.assertCountEqual(results, [2, 'conflict'])
        self.assertEqual(load_profile(self.database, 'default').profile_revision, 2)

    def test_corrupt_profile_identity_is_not_treated_as_missing_during_dry_run(self):
        saved = import_preferences(self.database, read_preferences(self.path), updated_at=NOW)
        document = editable_profile(saved)
        with Store(self.database) as store, store.db:
            store.db.execute('DROP TRIGGER immutable_candidate_profiles_update')
            payload = canonical_json(saved.model_dump() | {'profile_id': 'different'})
            store.db.execute('UPDATE candidate_profiles SET payload = ?', (payload,))
        with self.assertRaisesRegex(StoreError, 'identity'):
            import_preferences(self.database, document, updated_at=NOW, dry_run=True)
