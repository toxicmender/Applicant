"""Versioned operation and route contracts for provider acquisition."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

OperationName = Literal[
    'job_search',
    'job_detail',
    'company_profile',
    'compensation',
    'workplace_reviews',
    'workplace_policy',
    'account_setup',
]
Destination = Literal['display', 'persist', 'export']
Credential = Literal['none', 'browser_session', 'api_key', 'oauth2']


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


class Validation(Contract):
    outcome: Literal['unverified', 'passed', 'empty', 'blocked', 'failed', 'inconclusive'] = (
        'unverified'
    )
    checked_at: datetime | None = None
    revision: str | None = None
    reason: str | None = None
    last_passed_at: datetime | None = None
    freshness: Literal['unverified', 'fresh', 'stale'] = 'unverified'


class Runtime(Contract):
    health: str = 'unknown'
    reason: str | None = None
    eligible_at: datetime | None = None
    lease_until: datetime | None = None
    credential_status: Literal[
        'not_required',
        'unknown',
        'missing',
        'present_unverified',
        'valid',
        'expired',
        'invalid',
        'insufficient_scope',
    ] = 'unknown'
    credential_expires_at: datetime | None = None


class Route(Contract):
    id: str = Field(pattern=r'^[a-z][a-z0-9_]*$')
    priority: int
    method: Literal[
        'public_feed',
        'public_structured',
        'public_browser',
        'managed_browser',
        'official_api',
        'official_mcp',
        'user_import',
    ]
    credential_kind: Credential = 'none'
    policy: Literal['allowed', 'approval_required', 'prohibited', 'unreviewed']
    authority: str
    reviewed_at: datetime | None = None
    destinations: tuple[Destination, ...] = ()
    retention: Literal['none', 'temporary', 'durable'] = 'none'
    retention_ttl_seconds: int | None = Field(default=None, gt=0)
    adapter_revision: str
    fallback: str | None = None
    fixture: Validation = Validation()
    live: Validation = Validation()
    runtime: Runtime = Runtime()

    @model_validator(mode='after')
    def validate_retention(self):
        if (self.retention == 'temporary') != (self.retention_ttl_seconds is not None):
            raise ValueError('only temporary routes declare a retention TTL')
        if self.retention != 'durable' and set(self.destinations) - {'display'}:
            raise ValueError('persistent destinations require durable retention')
        return self


class Operation(Contract):
    implementation: Literal['implemented', 'import_only', 'pending', 'deprecated']
    routes: tuple[Route, ...]

    @model_validator(mode='after')
    def unique_routes(self):
        ids = {route.id for route in self.routes}
        if len(ids) != len(self.routes) or len({route.priority for route in self.routes}) != len(
            ids
        ):
            raise ValueError('operation route IDs and priorities must be unique')
        if any(
            route.fallback is not None and (route.fallback == route.id or route.fallback not in ids)
            for route in self.routes
        ):
            raise ValueError('fallback must name a different declared route')
        return self


class CatalogOperations(BaseModel):
    model_config = ConfigDict(extra='allow')

    id: str
    operations: dict[OperationName, Operation]


class SourcesDocument(Contract):
    operations_schema_version: Literal[1] = 1
    providers: tuple[CatalogOperations, ...]


def eligible_route(operation: Operation, destination: Destination, *, pin=None, has_input=False):
    """Return the selected route and sanitized exclusions in priority order."""
    exclusions = {}
    for route in sorted(operation.routes, key=lambda candidate: candidate.priority):
        if pin is not None and route.id != pin:
            continue
        state = route.runtime
        if route.method == 'user_import' and pin is None and not has_input:
            exclusions[route.id] = 'input_required'
        elif route.policy != 'allowed':
            exclusions[route.id] = route.policy
        elif operation.implementation != 'implemented' and route.method != 'user_import':
            exclusions[route.id] = 'route_unavailable'
        elif destination not in route.destinations:
            exclusions[route.id] = 'destination_denied'
        elif state.health in ('cooldown', 'leased', 'adapter_degraded'):
            exclusions[route.id] = state.health
        elif state.credential_status in ('missing', 'invalid', 'insufficient_scope'):
            exclusions[route.id] = state.credential_status
        elif state.credential_status == 'expired' and route.credential_kind != 'oauth2':
            exclusions[route.id] = 'expired'
        else:
            return route, exclusions
    return None, exclusions
