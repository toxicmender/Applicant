"""Explicit MoSPI PLFS acquisition through the shared verified HTTP client."""

from collections.abc import Callable
from uuid import uuid4

import httpx

from ..domain.benchmarks import BenchmarkQuery, BenchmarkResponse, BenchmarkSnapshot
from ..domain.canonical import utc
from ..domain.contracts import now
from ..domain.mospi import (
    MAX_BYTES,
    PAGE_SIZE,
    parse_metadata,
    parse_observation,
    parse_page,
    validate_coverage,
    validate_query,
    validate_selection,
)
from ..errors import ConfigError, SourceError, Unparseable
from .http import HttpClient

API_URL = 'https://api.mospi.gov.in/api/plfs/'
ADAPTER_VERSION = 'mospi-plfs-1'


class MoSPIProvider:
    def __init__(self, *, client: httpx.Client | None = None, clock: Callable = now):
        self.client, self.clock = client, clock

    def _http(self):
        return HttpClient('mospi', client=self.client, timeout=30, retries=2, interval=0.25)

    def _capture(self, response):
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise SourceError(f'MoSPI HTTP {response.status_code}', source='mospi') from error
        if len(response.content) > MAX_BYTES:
            raise Unparseable('MoSPI response exceeds byte limit', source='mospi')
        return BenchmarkResponse.capture(str(response.request.url), response.content)

    def _metadata(self, http):
        indicators = self._capture(
            http.get(API_URL + 'getIndicatorListByFrequency', params={'frequency_code': '1'})
        )
        filters = self._capture(
            http.get(
                API_URL + 'getFilterByIndicatorId',
                params={'indicator_code': '6', 'frequency_code': '1'},
            )
        )
        try:
            metadata = parse_metadata(indicators, filters)
        except (ValueError, UnicodeError) as error:
            raise Unparseable(f'invalid PLFS metadata: {error}', source='mospi') from error
        return (indicators, filters), metadata

    def discover(self) -> dict:
        with self._http() as http:
            responses, metadata = self._metadata(http)
        return {
            'kind': 'benchmark_discovery',
            'schema_version': 1,
            'provider': 'mospi',
            'dataset': 'plfs',
            'adapter_version': ADAPTER_VERSION,
            'retrieved_at': utc(self.clock()),
            'indicator': metadata['indicator'],
            'dimensions': metadata['dimensions'],
            'responses': [r.model_dump() for r in responses],
        }

    def fetch(self, query: BenchmarkQuery) -> BenchmarkSnapshot:
        as_of = utc(self.clock()).date()
        try:
            query = validate_query(
                BenchmarkQuery.model_validate_json(query.model_dump_json()), as_of=as_of
            )
        except ValueError as error:
            raise ConfigError(str(error)) from error
        with self._http() as http:
            responses, metadata = self._metadata(http)
            try:
                validate_selection(query, metadata)
            except ValueError as error:
                raise ConfigError(str(error)) from error
            params = {
                'indicator_code': '6',
                'frequency_code': '1',
                'Format': 'JSON',
                'limit': str(PAGE_SIZE),
            }
            params.update(
                {k if k == 'year' else k + '_code': ','.join(v) for k, v in query.filters.items()}
            )
            pages, page, totals, observations = 1, 1, None, {}
            while page <= pages:
                raw = self._capture(
                    http.get(API_URL + 'getData', params=params | {'page': str(page)})
                )
                try:
                    rows, current = parse_page(raw, page=page)
                    if totals is not None and current != totals:
                        raise ValueError('PLFS pagination totals changed during acquisition')
                    totals, pages = current, current[1]
                    for row in rows:
                        observation = parse_observation(row, raw, query, metadata, as_of=as_of)
                        if observation.observation_id in observations:
                            raise ValueError('duplicate PLFS cohort across pages')
                        observations[observation.observation_id] = observation
                except (ValueError, UnicodeError) as error:
                    raise Unparseable(f'invalid PLFS data: {error}', source='mospi') from error
                responses = (*responses, raw)
                page += 1
            try:
                validate_coverage(query, tuple(observations.values()))
            except ValueError as error:
                raise Unparseable(f'incomplete PLFS selection: {error}', source='mospi') from error
        return BenchmarkSnapshot(
            snapshot_id=str(uuid4()),
            query=query,
            adapter_version=ADAPTER_VERSION,
            retrieved_at=utc(self.clock()),
            release_label=f'PLFS {query.filters["year"][0]}; publication date unknown',
            responses=responses,
            observations=tuple(observations[k] for k in sorted(observations)),
        )
