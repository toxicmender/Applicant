"""Eurostat acquisition through shared HTTP; network work precedes persistence."""

from collections.abc import Callable
from uuid import uuid4

import httpx

from ..domain.benchmarks import BenchmarkQuery, BenchmarkResponse, BenchmarkSnapshot
from ..domain.canonical import utc
from ..domain.contracts import now
from ..domain.eurostat import DATASET, MAX_BYTES, decode_cube, observations, resolve_query
from ..errors import ConfigError, SourceError, Unparseable
from .http import HttpClient

URL = f'https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/{DATASET}'


class EurostatProvider:
    def __init__(self, *, client: httpx.Client | None = None, clock: Callable = now):
        self.client = client
        self.clock = clock

    def _request(self, http, params):
        response = http.get(URL, params=params)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise SourceError(f'Eurostat HTTP {response.status_code}', source='eurostat') from error
        if len(response.content) > MAX_BYTES:
            raise Unparseable('Eurostat response exceeds byte limit', source='eurostat')
        raw = BenchmarkResponse.capture(str(response.request.url), response.content)
        try:
            cube = decode_cube(response.content)
        except (ValueError, UnicodeError) as error:
            raise Unparseable(f'invalid Eurostat response: {error}', source='eurostat') from error
        return raw, cube

    def _http(self):
        return HttpClient('eurostat', client=self.client, timeout=30, retries=2, interval=0.25)

    def discover(self) -> dict:
        with self._http() as http:
            raw, cube = self._request(http, {'lang': 'EN'})
        return {
            'kind': 'benchmark_discovery',
            'schema_version': 1,
            'provider': 'eurostat',
            'dataset': DATASET,
            'retrieved_at': utc(self.clock()),
            'adapter_version': 'eurostat-ses-1',
            'dimensions': cube.payload['dimension'],
            'updated': cube.payload.get('updated'),
            'response': raw.model_dump(),
            'response_id': raw.response_id,
        }

    def fetch(self, query: BenchmarkQuery) -> BenchmarkSnapshot:
        query = BenchmarkQuery.model_validate_json(query.model_dump_json())
        if query.provider != 'eurostat' or query.dataset != DATASET:
            raise ConfigError('Eurostat supports only earn_ses22_28')
        with self._http() as http:
            metadata, structure = self._request(http, {'lang': 'EN'})
            try:
                resolved = resolve_query(query, structure)
            except ValueError as error:
                raise ConfigError(str(error)) from error
            params = [
                ('lang', 'EN'),
                *((k, v) for k in sorted(resolved.filters) for v in resolved.filters[k]),
            ]
            raw, cube = self._request(http, params)
            try:
                values = observations(cube, resolved, raw)
            except ValueError as error:
                raise Unparseable(
                    f'invalid Eurostat selection: {error}', source='eurostat'
                ) from error
        return BenchmarkSnapshot(
            snapshot_id=str(uuid4()),
            query=resolved,
            adapter_version='eurostat-ses-1',
            retrieved_at=utc(self.clock()),
            release_label=str(cube.payload.get('updated') or 'dataset update unknown'),
            responses=(metadata, raw),
            observations=values,
        )
