"""Mocked provider boundaries: exact tokens, dates, malformed data and ownership."""

import hashlib
import json
import unittest
from datetime import datetime
from decimal import Decimal

import httpx

from applicant.domain.canonical import canonical_json
from applicant.errors import Blocked, SourceError, Unparseable, Unreachable
from applicant.infra.fx import ECB_URL, FrankfurterFX, parse_ecb_response
from tests.ranking_fixtures import FIXTURES, NOW


class FXAcquisitionTest(unittest.TestCase):
    def test_mocked_batch_request_preserves_exact_bytes_and_decimal_tokens(self):
        raw = (FIXTURES / 'ecb-rates.json').read_bytes()
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, content=raw)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = FrankfurterFX(client=client).fetch(retrieved_at=NOW)
            self.assertFalse(client.is_closed)
        self.assertEqual(len(calls), 1)
        self.assertEqual(str(calls[0].url), ECB_URL + '?base=EUR')
        snapshot = result.snapshot
        self.assertEqual(snapshot.rates['USD'], Decimal('1.234567890123456789'))
        self.assertNotIn('JPY', snapshot.rates)
        self.assertEqual(result.diagnostics, ('fx_older_observation_omitted:JPY',))
        self.assertEqual(snapshot.response_digest, hashlib.sha256(raw).hexdigest())
        self.assertEqual(snapshot.raw_response, raw.decode('utf-8'))
        self.assertEqual(type(snapshot).model_validate_json(canonical_json(snapshot)), snapshot)

    def test_identity_row_need_not_be_present_and_equal_duplicates_are_safe(self):
        row = {'date': '2026-10-30', 'base': 'EUR', 'quote': 'USD', 'rate': 1.25}
        result = parse_ecb_response(
            json.dumps([row, row]).encode(), retrieved_at=NOW, request_url=ECB_URL
        )
        self.assertEqual(result.snapshot.rates, {'EUR': Decimal(1), 'USD': Decimal('1.25')})

    def test_invalid_payloads_are_source_errors(self):
        base = {'date': '2026-10-30', 'base': 'EUR', 'quote': 'USD', 'rate': 1.25}
        # Keep malformed structural cases separate from row mutations for readable failures.
        cases = [b'bad', b'\xff', b'{}', b'[]', b'[1]', b'[{"base":"EUR","base":"USD"}]', b'[NaN]']
        cases += [
            json.dumps([base | change]).encode()
            for change in (
                {'base': 'USD'},
                {'quote': None},
                {'quote': 'ZZZ'},
                {'quote': 'usd'},
                {'date': '2026-11-03'},
                {'date': '2026-02-30'},
                {'date': '20261030'},
                {'date': None},
                {'rate': 0},
                {'rate': -1},
                {'rate': '1.25'},
                {'rate': True},
                {'rate': float('inf')},
                {'quote': 'EUR', 'rate': 2},
                {'quote': 'EUR', 'rate': 1},
            )
        ]
        cases.append(json.dumps([base, base | {'rate': 2}]).encode())
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(Unparseable):
                parse_ecb_response(raw, retrieved_at=NOW, request_url=ECB_URL)

    def test_http_and_transport_failures_are_bounded(self):
        for status, exception in ((429, Blocked), (503, SourceError), (404, SourceError)):
            calls = []

            def handler(request, calls=calls, status=status):
                calls.append(request)
                return httpx.Response(status)

            with (
                httpx.Client(transport=httpx.MockTransport(handler)) as client,
                self.assertRaises(exception),
            ):
                FrankfurterFX(client=client).fetch(retrieved_at=NOW)
            self.assertEqual(len(calls), 1)

        def timeout(request):
            raise httpx.ReadTimeout('offline', request=request)

        with (
            httpx.Client(transport=httpx.MockTransport(timeout)) as client,
            self.assertRaises(Unreachable),
        ):
            FrankfurterFX(client=client).fetch(retrieved_at=NOW)

    def test_naive_acquisition_time_is_a_caller_error(self):
        with self.assertRaises(ValueError):
            parse_ecb_response(b'[]', retrieved_at=datetime(2026, 11, 2), request_url=ECB_URL)
