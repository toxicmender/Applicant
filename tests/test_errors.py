"""applicant.errors: one hierarchy, with the old names still in place."""

from __future__ import annotations

import unittest

import httpx

from applicant import errors, financials, models, reviews


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


class AliasTest(unittest.TestCase):
    """The names every caller already imports are the shared classes."""

    def test_job_board_names(self):
        self.assertIs(models.JobsError, errors.SourceError)
        self.assertIs(models.BlockedError, errors.Blocked)

    def test_reviews_names(self):
        self.assertIs(reviews.ReviewsError, errors.SourceError)
        self.assertIs(reviews.ChallengeError, errors.Blocked)
        self.assertIs(reviews.CompanyNotFound, errors.NotFound)
        self.assertIs(reviews.ParseError, errors.Unparseable)

    def test_financials_names(self):
        self.assertIs(financials.FinancialsError, errors.SourceError)
        self.assertIs(financials.ChallengeError, errors.Blocked)
        self.assertIs(financials.CompanyNotFound, errors.NotFound)
        self.assertIs(financials.ParseError, errors.Unparseable)
        self.assertIs(financials.AuthError, errors.AuthFailed)
        self.assertIs(financials.QuotaExhausted, errors.QuotaExhausted)

    def test_a_bot_check_is_one_thing_whichever_area_hit_it(self):
        with self.assertRaises(reviews.ChallengeError):
            raise models.BlockedError('Naukri served a bot check')


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
