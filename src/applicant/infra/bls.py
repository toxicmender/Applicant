"""BLS code-table discovery and one bounded public wage API request."""

from collections.abc import Callable
from uuid import uuid4

import httpx

from ..domain.benchmarks import BenchmarkQuery, BenchmarkResponse, BenchmarkSnapshot
from ..domain.bls import (
    MAX_BYTES,
    TABLES,
    parse_observations,
    parse_table,
    series_plan,
    validate_query,
)
from ..domain.canonical import utc
from ..domain.contracts import now
from ..errors import ConfigError, SourceError, Unparseable
from .http import HttpClient

TABLE_URL = 'https://download.bls.gov/pub/time.series/oe/oe.'
API_URL = 'https://api.bls.gov/publicAPI/v2/timeseries/data/'


class BLSProvider:
    def __init__(self, *, client: httpx.Client | None = None, clock: Callable = now):
        self.client, self.clock = client, clock

    def _http(self):
        return HttpClient('bls', client=self.client, timeout=30, retries=2, interval=0.25)

    def _capture(self, response):
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise SourceError(f'BLS HTTP {response.status_code}', source='bls') from error
        if len(response.content) > MAX_BYTES:
            raise Unparseable('BLS response exceeds byte limit', source='bls')
        return BenchmarkResponse.capture(str(response.request.url), response.content)

    def _metadata(self, http):
        responses, tables = [], {}
        for name in TABLES:
            raw = self._capture(http.get(TABLE_URL + name))
            try:
                tables[name] = parse_table(raw.body, name)
            except (ValueError, UnicodeError) as error:
                raise Unparseable(f'invalid BLS {name} table: {error}', source='bls') from error
            responses.append(raw)
        return tuple(responses), tables

    def discover(self) -> dict:
        with self._http() as http:
            responses, tables = self._metadata(http)
        return {
            'kind': 'benchmark_discovery',
            'schema_version': 1,
            'provider': 'bls',
            'dataset': 'oews',
            'retrieved_at': utc(self.clock()),
            'adapter_version': 'bls-oews-1',
            'dimensions': tables,
            'responses': [r.model_dump() for r in responses],
        }

    def fetch(self, query: BenchmarkQuery) -> BenchmarkSnapshot:
        try:
            query = validate_query(
                BenchmarkQuery.model_validate_json(query.model_dump_json()),
                as_of=utc(self.clock()).date(),
            )
        except ValueError as error:
            raise ConfigError(str(error)) from error
        with self._http() as http:
            metadata, tables = self._metadata(http)
            try:
                plan = series_plan(query, tables)
            except ValueError as error:
                raise ConfigError(str(error)) from error
            year = query.filters['year'][0]
            raw = self._capture(
                http.post(
                    API_URL, json={'seriesid': list(plan), 'startyear': year, 'endyear': year}
                )
            )
            try:
                values = parse_observations(raw, query, plan)
            except (ValueError, UnicodeError) as error:
                raise Unparseable(f'invalid BLS API response: {error}', source='bls') from error
        return BenchmarkSnapshot(
            snapshot_id=str(uuid4()),
            query=query,
            adapter_version='bls-oews-1',
            retrieved_at=utc(self.clock()),
            release_label=f'OEWS May {year}; publication date unknown',
            responses=(*metadata, raw),
            observations=values,
        )
