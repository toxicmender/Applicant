"""Explicit benchmark acquisition; persist only a validated completed response."""

from pathlib import Path

from ..domain.benchmarks import BenchmarkProvider, BenchmarkQuery
from ..errors import ConfigError
from ..infra.bls import BLSProvider
from ..infra.eurostat import EurostatProvider
from ..infra.mospi import MoSPIProvider
from ..infra.store.ranking_common import validated
from .benchmarks import import_snapshot


def provider(name: str) -> BenchmarkProvider:
    if name == 'eurostat':
        return EurostatProvider()
    if name == 'bls':
        return BLSProvider()
    if name == 'mospi':
        return MoSPIProvider()
    raise ConfigError('supported benchmark providers: eurostat, bls, mospi')


def discover_benchmarks(name: str) -> dict:
    return provider(name).discover()


def fetch_benchmarks(database: Path, query: BenchmarkQuery) -> dict:
    query = validated(query)
    snapshot = validated(provider(query.provider).fetch(query))
    if (
        snapshot.query.provider != query.provider
        or snapshot.query.dataset != query.dataset
        or any(snapshot.query.filters.get(k) != v for k, v in query.filters.items())
    ):
        raise ValueError('provider changed the requested benchmark query')
    # Provider work and validation finish before any durable writer is opened.
    saved = import_snapshot(database, snapshot)
    return {
        'kind': 'benchmark_acquisition_result',
        'schema_version': 1,
        'snapshot': saved.model_dump(),
    }
