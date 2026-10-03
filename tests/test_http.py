"""applicant.infra.http: the one retry loop, and what it turns failures into."""

from __future__ import annotations

import unittest
from contextlib import suppress
from unittest import mock

import httpx

from applicant import log
from applicant.errors import Blocked, SourceError, Unreachable
from applicant.infra import http as http_module
from applicant.infra.http import HttpClient


def client(handler, **options) -> HttpClient:
    options.setdefault('backoff', 0)
    options.setdefault('interval', 0)
    return HttpClient(
        'Example', client=httpx.Client(transport=httpx.MockTransport(handler)), **options
    )


def counting(*responses):
    """A handler answering with each response in turn, recording the calls."""
    calls = []

    def handler(request):
        calls.append(request)
        answer = responses[min(len(calls), len(responses)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer

    return handler, calls


class RetryTest(unittest.TestCase):
    def test_a_good_answer_is_returned_first_time(self):
        handler, calls = counting(httpx.Response(200, text='ok'))
        self.assertEqual(client(handler).get('https://example.test/').text, 'ok')
        self.assertEqual(len(calls), 1)

    def test_throttling_statuses_are_retried(self):
        for status in (429, 502, 503, 504):
            with self.subTest(status=status):
                handler, calls = counting(httpx.Response(status), httpx.Response(200))
                self.assertEqual(client(handler).get('https://example.test/').status_code, 200)
                self.assertEqual(len(calls), 2)

    def test_a_server_error_is_handed_back_not_retried(self):
        """A 500 is usually deterministic; the adapter decides what it means."""
        handler, calls = counting(httpx.Response(500))
        self.assertEqual(client(handler).get('https://example.test/').status_code, 500)
        self.assertEqual(len(calls), 1)

    def test_a_404_is_the_callers_to_judge(self):
        handler, _ = counting(httpx.Response(404))
        self.assertEqual(client(handler).get('https://example.test/').status_code, 404)

    def test_a_flaky_network_is_retried(self):
        handler, calls = counting(httpx.ConnectError('reset'), httpx.Response(200))
        self.assertEqual(client(handler).get('https://example.test/').status_code, 200)
        self.assertEqual(len(calls), 2)

    def test_retries_are_bounded(self):
        handler, calls = counting(httpx.Response(503))
        client(handler, retries=4).get('https://example.test/')
        self.assertEqual(len(calls), 4)

    def test_zero_retries_still_makes_one_attempt(self):
        handler, calls = counting(httpx.Response(200))
        client(handler, retries=0).get('https://example.test/')
        self.assertEqual(len(calls), 1)

    def test_retry_when_catches_an_error_dressed_as_success(self):
        """The World Bank throttles with HTTP 200 and an error body."""
        handler, calls = counting(httpx.Response(200, text='throttled'), httpx.Response(200))
        answer = client(handler, retry_when=lambda response: response.text == 'throttled').get(
            'https://example.test/'
        )
        self.assertEqual(answer.text, '')
        self.assertEqual(len(calls), 2)


class FailureTest(unittest.TestCase):
    def test_a_network_that_never_answers_is_unreachable(self):
        handler, calls = counting(httpx.ConnectError('network is unreachable'))
        with self.assertRaisesRegex(Unreachable, 'could not reach Example'):
            client(handler, retries=2).get('https://example.test/')
        self.assertEqual(len(calls), 2)

    def test_unreachable_names_its_source(self):
        handler, _ = counting(httpx.ConnectError('down'))
        with self.assertRaises(Unreachable) as raised:
            client(handler, retries=1).get('https://example.test/')
        self.assertEqual(raised.exception.source, 'example')

    def test_a_rate_limit_that_outlasts_the_retries_is_blocked(self):
        handler, calls = counting(httpx.Response(429))
        with self.assertRaises(Blocked):
            client(handler, retries=3).get('https://example.test/')
        self.assertEqual(len(calls), 3)

    def test_a_rate_limit_not_retried_is_blocked_at_once(self):
        """LinkedIn: carrying on through a 429 is how a scrape gets blocked."""
        handler, calls = counting(httpx.Response(429))
        with self.assertRaises(Blocked):
            client(handler, retry_on=(502, 503)).get('https://example.test/')
        self.assertEqual(len(calls), 1)

    def test_other_exhausted_statuses_come_back_as_responses(self):
        handler, _ = counting(httpx.Response(503))
        self.assertEqual(client(handler, retries=2).get('https://example.test/').status_code, 503)

    def test_every_failure_is_a_source_error(self):
        for answer in (httpx.ConnectError('down'), httpx.Response(429)):
            with self.subTest(answer=answer):
                handler, _ = counting(answer)
                with self.assertRaises(SourceError):
                    client(handler, retries=1).get('https://example.test/')


class BackoffTest(unittest.TestCase):
    def sleeps(self, handler, **options) -> list[float]:
        with mock.patch.object(http_module.time, 'sleep') as sleep, suppress(SourceError):
            client(handler, **options).get('https://example.test/')
        return [call.args[0] for call in sleep.call_args_list]

    def test_the_wait_grows_between_attempts(self):
        handler, _ = counting(httpx.Response(503))
        waits = self.sleeps(handler, retries=3, backoff=1.0)
        self.assertEqual(len(waits), 2)
        self.assertTrue(2 <= waits[0] <= 3, waits)  # 1 * 2**1, plus up to half again
        self.assertTrue(4 <= waits[1] <= 6, waits)

    def test_a_zero_backoff_never_sleeps(self):
        handler, _ = counting(httpx.Response(503))
        self.assertEqual(self.sleeps(handler, retries=3, backoff=0), [0.0, 0.0])

    def test_retry_after_is_honoured(self):
        handler, _ = counting(
            httpx.Response(429, headers={'Retry-After': '7'}), httpx.Response(200)
        )
        self.assertEqual(self.sleeps(handler, backoff=1.0), [7.0])

    def test_an_absurd_retry_after_falls_back_to_the_backoff(self):
        """A server asking for an hour does not get to stall the run that long."""
        handler, _ = counting(
            httpx.Response(429, headers={'Retry-After': '3600'}), httpx.Response(200)
        )
        (wait,) = self.sleeps(handler, backoff=1.0)
        self.assertLess(wait, 60)


class PacingTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(http_module._next_allowed, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_requests_to_one_host_are_spaced(self):
        handler, _ = counting(httpx.Response(200))
        paced = client(handler, interval=2.0)
        with (
            mock.patch.object(http_module.time, 'monotonic', return_value=100.0),
            mock.patch.object(http_module.time, 'sleep') as sleep,
        ):
            paced.get('https://example.test/a')
            paced.get('https://example.test/b')
        sleep.assert_called_once_with(2.0)

    def test_the_gap_is_shared_by_every_client_in_the_process(self):
        """Four boards must not be able to burst one host between them."""
        handler, _ = counting(httpx.Response(200))
        with (
            mock.patch.object(http_module.time, 'monotonic', return_value=100.0),
            mock.patch.object(http_module.time, 'sleep') as sleep,
        ):
            client(handler, interval=2.0).get('https://example.test/a')
            client(handler, interval=2.0).get('https://example.test/b')
        sleep.assert_called_once_with(2.0)

    def test_different_hosts_do_not_wait_for_each_other(self):
        handler, _ = counting(httpx.Response(200))
        paced = client(handler, interval=2.0)
        with (
            mock.patch.object(http_module.time, 'monotonic', return_value=100.0),
            mock.patch.object(http_module.time, 'sleep') as sleep,
        ):
            paced.get('https://one.test/')
            paced.get('https://two.test/')
        sleep.assert_not_called()


class LifecycleTest(unittest.TestCase):
    def test_a_secret_is_registered_for_masking(self):
        self.addCleanup(log._secrets.clear)
        HttpClient('Example', secret='api-key-0123456789', interval=0).close()
        self.assertIn('api-key-0123456789', log._secrets)

    def test_a_client_handed_in_is_left_open(self):
        """Its owner - a test, usually - decides when it closes."""
        given = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
        HttpClient('Example', client=given).close()
        self.assertFalse(given.is_closed)

    def test_a_client_it_made_is_closed(self):
        with HttpClient('Example') as owned:
            pass
        self.assertTrue(owned.client.is_closed)


if __name__ == '__main__':
    unittest.main()
