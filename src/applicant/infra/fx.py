"""Ranking-only Frankfurter v2 ECB acquisition with exact Decimal provenance."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, DecimalException
from uuid import uuid4

import httpx

from ..domain.canonical import utc
from ..domain.contracts import currency
from ..domain.ranking_v2 import FXSnapshotV2
from ..errors import SourceError, Unparseable
from .http import HttpClient

ECB_URL = 'https://api.frankfurter.dev/v2/providers/ECB/rates'


@dataclass(frozen=True)
class AcquiredFX:
    snapshot: FXSnapshotV2
    diagnostics: tuple[str, ...] = ()


def _object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON property')
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ValueError(f'nonfinite JSON number: {value}')


def parse_ecb_response(raw: bytes, *, retrieved_at: datetime, request_url: str) -> AcquiredFX:
    """Validate the entire response, then retain the latest non-identity date.

    The caller supplies the acquisition timestamp; no evaluation clock is read by
    the decoder. Original UTF-8 response text and its byte digest are preserved.
    """
    retrieved_at = utc(retrieved_at)
    try:
        text = raw.decode('utf-8')
        payload = json.loads(
            text,
            parse_float=Decimal,
            parse_int=Decimal,
            parse_constant=_constant,
            object_pairs_hook=_object,
        )
        if not isinstance(payload, list) or not payload:
            raise ValueError('expected nonempty currency-pair array')
        quotes: dict[tuple[date, str], Decimal] = {}
        for row in payload:
            if not isinstance(row, dict) or row.get('base') != 'EUR':
                raise ValueError('expected EUR-based pair')
            quote = row.get('quote')
            if not isinstance(quote, str):
                raise ValueError('missing quote currency')
            currency(quote)
            day = row.get('date')
            if not isinstance(day, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
                raise ValueError('expected ISO observation date')
            effective = date.fromisoformat(day)
            if effective > retrieved_at.date():
                raise ValueError('future observation date')
            rate = row.get('rate')
            if not isinstance(rate, Decimal) or not rate.is_finite() or rate <= 0:
                raise ValueError('expected positive finite numeric rate')
            if quote == 'EUR' and rate != 1:
                raise ValueError('identity rate must be one')
            key = effective, quote
            if key in quotes and quotes[key] != rate:
                raise ValueError('conflicting duplicate pair')
            quotes[key] = rate
        days = [day for day, quote in quotes if quote != 'EUR']
        if not days:
            raise ValueError('no non-identity rates')
        selected = max(days)
        rates = {quote: rate for (day, quote), rate in quotes.items() if day == selected}
        rates['EUR'] = Decimal(1)
        omitted = sorted({quote for day, quote in quotes if day != selected and quote != 'EUR'})
        snapshot = FXSnapshotV2(
            snapshot_id=str(uuid4()),
            base_currency='EUR',
            source='frankfurter-v2-ECB',
            captured_at=retrieved_at,
            retrieved_at=retrieved_at,
            effective_date=selected,
            provenance='live',
            rates=rates,
            provider='ECB',
            request_url=request_url,
            raw_response=text,
            response_digest=hashlib.sha256(raw).hexdigest(),
        )
        return AcquiredFX(
            snapshot, tuple(f'fx_older_observation_omitted:{code}' for code in omitted)
        )
    except (ValueError, UnicodeError, DecimalException) as error:
        raise Unparseable(f'invalid ECB rate response: {error}', source='frankfurter') from error


class FrankfurterFX:
    def __init__(self, *, client: httpx.Client | None = None, timeout: float = 20.0):
        self.client = client
        self.timeout = timeout

    def fetch(self, *, retrieved_at: datetime) -> AcquiredFX:
        # One bounded attempt per batch. HttpClient preserves injected client ownership.
        with HttpClient(
            'frankfurter', client=self.client, timeout=self.timeout, retries=1, interval=0
        ) as http:
            response = http.get(ECB_URL, params={'base': 'EUR'})
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as error:
                raise SourceError(
                    f'ECB provider HTTP {response.status_code}', source='frankfurter'
                ) from error
            return parse_ecb_response(
                response.content, retrieved_at=retrieved_at, request_url=str(response.request.url)
            )
