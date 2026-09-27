"""applicant.errors: one hierarchy, and the only place its names live."""

from __future__ import annotations

import importlib
import unittest

import httpx

from applicant import errors, financials, reviews


class HierarchyTest(unittest.TestCase):
    def test_every_source_failure_is_a_source_error(self):
        for kind in (
            errors.Blocked,
            errors.NotFound,
            errors.Unparseable,
            errors.AuthFailed,
            errors.QuotaExhausted,
            errors.Unreachable,
        ):
            with self.subTest(kind=kind.__name__):
                self.assertTrue(issubclass(kind, errors.SourceError))

    def test_running_out_of_credits_is_an_auth_failure(self):
        """So code handling a refused key already handles an empty quota."""
        self.assertTrue(issubclass(errors.QuotaExhausted, errors.AuthFailed))

    def test_everything_shares_one_base(self):
        for kind in (errors.SourceError, errors.StoreError, errors.ConfigError):
            with self.subTest(kind=kind.__name__):
                self.assertTrue(issubclass(kind, errors.ApplicantError))

    def test_a_source_is_optional_and_the_message_stands_alone(self):
        self.assertIsNone(errors.SourceError('down').source)
        error = errors.Blocked('bot check', source='naukri')
        self.assertEqual((str(error), error.source), ('bot check', 'naukri'))


class OldNamesGoneTest(unittest.TestCase):
    """The 0.1.x aliases and shim modules were removed in 0.2.0."""

    def test_the_areas_no_longer_carry_their_own_error_names(self):
        for module, names in (
            (reviews, ('ReviewsError', 'ChallengeError', 'CompanyNotFound', 'ParseError')),
            (financials, ('FinancialsError', 'AuthError', 'ChallengeError', 'ParseError')),
        ):
            for name in names:
                with self.subTest(module=module.__name__, name=name):
                    self.assertFalse(hasattr(module, name))

    def test_the_shim_modules_are_gone(self):
        for name in (
            'applicant.models',
            'applicant.dates',
            'applicant.salary',
            'applicant.places',
            'applicant.browser',
            'applicant.reviews.errors',
            'applicant.financials.errors',
        ):
            with self.subTest(module=name), self.assertRaises(ModuleNotFoundError):
                importlib.import_module(name)


class RaisedTest(unittest.TestCase):
    def test_tracxn_out_of_credits_says_retrying_cannot_help(self):
        answer = httpx.Response(403, json={'errorCode': 900, 'message': 'API out of credits'})
        client = financials.TracxnClient(
            api_key='t',
            delay=0,
            client=httpx.Client(transport=httpx.MockTransport(lambda request: answer)),
        )
        with self.assertRaises(errors.QuotaExhausted):
            client.fetch('5c1c697b8f088f5b6f55226c')


if __name__ == '__main__':
    unittest.main()
