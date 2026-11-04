"""Scripted questionnaire correction, cancellation and concurrent edit journeys."""

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal, localcontext
from pathlib import Path
from unittest import mock

from applicant.cli import main
from applicant.domain.survey import PREFERENCE_FIELDS
from applicant.infra.preference_documents import read_preferences
from applicant.interaction import Scripted
from applicant.services.survey import editable_profile, import_preferences, load_profile
from applicant.services.survey_questionnaire import GROUPS, questionnaire
from tests.ranking_fixtures import NOW
from tests.test_survey import FIXTURE

ANSWERS = [
    '5',
    'Software engineering',
    'Senior, Staff',
    'Python, Python, Rust',
    'Europe',
    'Remote, Hybrid',
    '3 hours',
    'IN',
    'Remote',
    '60000',
    '90000',
    'EUR',
    'gross',
    'either',
    '',
    '',
    '',
    '',
    '',
    '',
    'yes',
]


class QuestionnaireTest(unittest.TestCase):
    def initial(self):
        return read_preferences(FIXTURE)

    def test_all_editable_fields_are_asked_and_validated_with_defaults_and_units(self):
        script = Scripted(*ANSWERS)
        with localcontext() as context:
            context.prec = 2
            document = questionnaire(script, profile_id='candidate')
        assert document is not None
        value = document.to_profile(updated_at=NOW)
        self.assertEqual(value.experience_years, Decimal(5))
        self.assertEqual(value.core_technologies, ('Python', 'Rust'))
        self.assertEqual(value.required_work_modes, ('Remote',))
        self.assertEqual(value.preferred_work_modes, ('Remote', 'Hybrid'))
        self.assertEqual(value.employment_preference, 'either')
        self.assertEqual(document.expected_revision, 0)
        self.assertEqual(script.answers, [])
        asked = {name.split('.')[0] for group in GROUPS for name, _ in group}
        self.assertEqual(asked, PREFERENCE_FIELDS)
        self.assertIn('annual base pay', script.said[0])
        self.assertIn('Country is where you work', script.said[0])
        self.assertIn('next revision 1', script.said[-1])

    def test_invalid_required_values_empty_list_items_and_unknown_enums_reprompt(self):
        answers = [
            '',
            '-',
            'NaN',
            '-1',
            '5',
            'Software',
            'Senior,,Staff',
            'Expert',
            'Senior',
            ' Python, Python , Rust ',
            '',
            'Remote',
            '',
            'XX',
            'IN',
            'Remote',
            '',
            '',
            'BAD',
            'EUR',
            'gross',
            'either',
            '',
            '',
            '',
            '',
            '',
            '',
            'maybe',
            'Y',
        ]
        script = Scripted(*answers)
        document = questionnaire(script, profile_id='default')
        assert document is not None
        self.assertEqual(document.expanded()['core_technologies'], ('Python', 'Rust'))
        self.assertTrue(any('cannot be cleared' in s for s in script.said))
        self.assertTrue(any('list items must not be empty' in s for s in script.said))
        self.assertEqual(script.said[-1], 'Please answer yes or no. Save? [y/N]: ')
        self.assertEqual(script.answers, [])

    def test_clear_optional_values_and_lists_and_keep_existing_values(self):
        answers = [
            '',
            '',
            '-',
            '-',
            '-',
            '-',
            '-',
            '-',
            '-',
            '-',
            '-',
            '',
            '',
            '',
            '',
            '',
            '',
            '',
            '',
            '',
            'yes',
        ]
        document = questionnaire(Scripted(*answers), profile_id='ignored', initial=self.initial())
        assert document is not None
        values = document.expanded()
        self.assertEqual(document.profile_id, 'default')
        self.assertEqual(values['experience_years'], Decimal('5.5'))
        self.assertIsNone(values['minimum_base_salary'])
        self.assertIsNone(values['work_country'])
        self.assertEqual(values['required_work_modes'], ())
        self.assertEqual(values['core_technologies'], ())

    def test_salary_and_weight_groups_reprompt_without_normalizing_or_losing_answers(self):
        # Salary group fails once; the six weights also fail once as a group.
        script = Scripted(
            *ANSWERS[:9],
            '100000',
            '90000',
            'EUR',
            'gross',
            '60000',
            '',
            '',
            '',
            'either',
            '0.5',
            '0.5',
            '0.5',
            '0.5',
            '0.5',
            '0.5',
            '2',
            '-',
            '0.25',
            '0.20',
            '0.20',
            '0.15',
            '0.10',
            '0.10',
            'yes',
        )
        document = questionnaire(script, profile_id='default')
        assert document is not None
        self.assertEqual(document.expanded()['minimum_base_salary'], Decimal(60000))
        self.assertEqual(document.expanded()['weights']['compensation'], Decimal('0.25'))
        self.assertEqual(sum('Please correct this group' in s for s in script.said), 2)
        self.assertEqual(document.expanded()['target_seniority'], ('Senior', 'Staff'))
        self.assertEqual(script.answers, [])

    def test_decline_empty_confirmation_and_eof_cancel(self):
        for reply in ('', 'n', 'NO'):
            with self.subTest(reply=reply):
                self.assertIsNone(questionnaire(Scripted(*ANSWERS[:-1], reply), profile_id='x'))
        for count in (0, 10, 20):
            with self.subTest(count=count):
                script = Scripted(*ANSWERS[:count])
                original = script.ask

                def ask(question, *, secret=False, script=script, original=original):
                    if not script.answers:
                        raise EOFError
                    return original(question, secret=secret)

                with mock.patch.object(script, 'ask', side_effect=ask):
                    self.assertIsNone(questionnaire(script, profile_id='x'))


class InteractiveCliTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.database = self.root / 'applicant.db'

    def cli(self, script, *flags):
        out, err = io.StringIO(), io.StringIO()
        with (
            redirect_stdout(out),
            redirect_stderr(err),
            mock.patch('applicant.cli.survey.sys.stdin.isatty', return_value=True),
            mock.patch('applicant.cli.survey.Terminal', return_value=script),
            mock.patch('applicant.cli.survey.now', return_value=NOW),
        ):
            code = main(['survey', 'edit', '--no-log-file', '--data-dir', str(self.root), *flags])
        return code, out.getvalue(), err.getvalue()

    def test_create_then_edit_exact_profile_and_preserve_original_revision(self):
        code, out, err = self.cli(Scripted(*ANSWERS), '--profile', 'engineer')
        self.assertEqual(code, 0)
        self.assertIn('saved profile engineer revision 1', out)
        self.assertEqual(err, '')
        original = load_profile(self.database, 'engineer')
        self.assertEqual(self.cli(Scripted(*([''] * 20), 'yes'), '--profile', 'engineer')[0], 0)
        self.assertEqual(load_profile(self.database, 'engineer', 1), original)
        self.assertEqual(load_profile(self.database, 'engineer').profile_revision, 2)

    def test_decline_and_eof_never_create_a_database_and_interrupt_returns_130(self):
        self.assertEqual(self.cli(Scripted(*ANSWERS[:-1], 'no'))[0], 0)
        self.assertFalse(self.database.exists())
        for error, expected in ((EOFError, 0), (KeyboardInterrupt, 130)):
            with self.subTest(error=error):
                script = mock.Mock()
                script.ask.side_effect = error
                self.assertEqual(self.cli(script)[0], expected)
                self.assertFalse(self.database.exists())

    def test_existing_profile_cancellation_preserves_bytes_and_closes_reader_before_prompt(self):
        saved = import_preferences(self.database, read_preferences(FIXTURE), updated_at=NOW)
        original = self.database.read_bytes()
        script = Scripted(*([''] * 20), 'no')
        # The read-only owner must already be closed on reaching the questionnaire.
        from applicant.infra.store.sqlite import Store

        opened = []
        opener = Store.open_readonly

        def capture(path):
            store = opener(path)
            opened.append(store)
            return store

        ask = script.ask

        def checked(question, *, secret=False):
            import sqlite3

            with self.assertRaises(sqlite3.ProgrammingError):
                opened[0].db.execute('SELECT 1')
            self.assertEqual(self.database.read_bytes(), original)
            return ask(question, secret=secret)

        with (
            mock.patch.object(Store, 'open_readonly', side_effect=capture),
            mock.patch.object(script, 'ask', side_effect=checked),
        ):
            self.assertEqual(self.cli(script)[0], 0)
        self.assertEqual(load_profile(self.database, 'default'), saved)

    def test_concurrent_edit_during_confirmation_conflicts_instead_of_overwriting(self):
        saved = import_preferences(self.database, read_preferences(FIXTURE), updated_at=NOW)
        script = Scripted(*([''] * 20), 'yes')
        ask = script.ask

        def changed(question, *, secret=False):
            if 'Save?' in question:
                import_preferences(self.database, editable_profile(saved), updated_at=NOW)
            return ask(question, secret=secret)

        with mock.patch.object(script, 'ask', side_effect=changed):
            code, _, err = self.cli(script)
        self.assertEqual(code, 2)
        self.assertIn('expected revision 1; current revision is 2', err)
        self.assertEqual(load_profile(self.database, 'default').profile_revision, 2)

    def test_terminal_prompts_stay_on_stderr_and_answers_are_not_json(self):
        out, err = io.StringIO(), io.StringIO()
        with (
            redirect_stdout(out),
            redirect_stderr(err),
            mock.patch('applicant.cli.survey.sys.stdin.isatty', return_value=True),
            mock.patch('builtins.input', side_effect=ANSWERS),
        ):
            code = main(['survey', 'edit', '--no-log-file', '--data-dir', str(self.root)])
        self.assertEqual(code, 0)
        self.assertIn('saved profile default', out.getvalue())
        self.assertNotIn('Save?', out.getvalue())
        self.assertIn('Save?', err.getvalue())
