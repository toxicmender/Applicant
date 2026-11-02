"""New-run FX acquisition/fallback, with no network inside a write transaction."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import uuid4

from ..domain.canonical import utc
from ..domain.contracts import currency
from ..domain.fx_resolution import FXResolution
from ..domain.ranking_v2 import FXSnapshotV2, ScoringConfigurationV2
from ..errors import SourceError, StoreError
from ..infra.fx import AcquiredFX, FrankfurterFX
from ..infra.store.ranking_codecs import FX
from ..infra.store.ranking_common import validated
from ..infra.store.snapshots import FXSnapshotRepository
from ..infra.store.sqlite import Store
from .ranking_history import save_fx


class FXProvider(Protocol):
    def fetch(self, *, retrieved_at: datetime) -> AcquiredFX: ...


def resolve_fx(
    store: Store,
    *,
    target_currency: str,
    source_currencies: set[str],
    config: ScoringConfigurationV2,
    evaluated_at: datetime,
    offline: bool = False,
    supplied: FX | None = None,
    static: FX | None = None,
    provider: FXProvider | None = None,
) -> FXResolution:
    """Select/persist one snapshot for a new batch, never combine quote tables.

    evaluated_at is the caller's explicit acquisition/batch timestamp. Historical
    reads use load_history instead. Explicit snapshots must satisfy this run's
    chronology/freshness/static policy; invalid caller inputs fail, not fall back.
    """
    if store.db.in_transaction:
        raise StoreError('FX resolution requires a connection without an active transaction')
    evaluated_at = utc(evaluated_at)
    currency(target_currency)
    wanted = {currency(code) for code in source_currencies} - {target_currency}
    config = validated(config)
    diagnostics: list[str] = []

    def fresh(snapshot: FX) -> bool:
        return snapshot.captured_at <= evaluated_at and (
            snapshot.provenance == 'identity'
            or 0 <= (evaluated_at.date() - snapshot.effective_date).days <= config.fx_max_age_days
        )

    def usable(snapshot: FX) -> bool:
        return target_currency in snapshot.rates and bool(wanted & snapshot.rates.keys())

    def finish(snapshot: FX) -> FXResolution:
        missing = sorted(
            wanted if target_currency not in snapshot.rates else wanted - snapshot.rates.keys()
        )
        saved = save_fx(store, snapshot)
        return FXResolution(
            snapshot=saved,
            missing_currencies=tuple(missing),
            diagnostics=tuple(diagnostics + [f'fx_missing_rate:{code}' for code in missing]),
        )

    def identity() -> FXResolution:
        snapshot = FXSnapshotV2.model_validate(
            {
                'snapshot_id': str(uuid4()),
                'base_currency': target_currency,
                'source': 'mathematical-identity',
                'captured_at': evaluated_at,
                'retrieved_at': evaluated_at,
                'effective_date': evaluated_at.date(),
                'provenance': 'identity',
                'rates': {target_currency: '1'},
            }
        )
        return finish(snapshot)

    if supplied is not None:
        supplied = validated(supplied)
        if not fresh(supplied):
            raise ValueError('supplied FX snapshot is future-dated or stale')
        if supplied.provenance == 'static' and not config.allow_static_fx:
            raise ValueError('static FX snapshots require explicit opt-in')
        diagnostics.append('fx_supplied')
        return finish(supplied)
    if static is not None:
        static = validated(static)
        if static.provenance != 'static':
            raise ValueError('static fallback must have static provenance')
    if not wanted:
        diagnostics.append('fx_identity')
        return identity()
    if not offline:
        try:
            acquired = (FrankfurterFX() if provider is None else provider).fetch(
                retrieved_at=evaluated_at
            )
        except SourceError as error:
            diagnostics.append(f'fx_provider_unavailable:{type(error).__name__.lower()}')
        else:
            snapshot = validated(acquired.snapshot)
            diagnostics.extend(acquired.diagnostics)
            if fresh(snapshot) and usable(snapshot):
                diagnostics.append('fx_live')
                return finish(snapshot)
            diagnostics.append('fx_live_unusable')
    else:
        diagnostics.append('fx_offline')
    cached = FXSnapshotRepository(store.db).latest_eligible(
        evaluated_at=evaluated_at,
        max_age_days=config.fx_max_age_days,
        target_currency=target_currency,
        source_currencies=wanted,
    )
    if cached is not None:
        # Preserve original observation/retrieval and response metadata, even for
        # older v1 durable inputs. This never reads the legacy float search cache.
        data = cached.model_dump() | {
            'schema_version': 2,
            'snapshot_id': str(uuid4()),
            'provenance': 'cache',
            'captured_at': evaluated_at,
            'parent_snapshot_id': cached.snapshot_id,
        }
        diagnostics.append('fx_cache')
        return finish(FXSnapshotV2.model_validate(data))
    if static is not None:
        if config.allow_static_fx and fresh(static) and usable(static):
            diagnostics.append('fx_static')
            return finish(static)
        diagnostics.append('fx_static_unusable')
    diagnostics.append('fx_unavailable')
    return identity()
