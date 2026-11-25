from datetime import datetime, timedelta, timezone

from applicant.services.provider_health import acquire, finish, route_health

NOW = datetime(2026, 11, 25, tzinfo=timezone.utc)
ROUTE = ('job_search', 'public')


def test_parser_recovery_requires_a_different_adapter_revision(tmp_path):
    database = tmp_path / 'state.db'
    clock = lambda: NOW  # noqa: E731
    name = 'wellfound'
    old = (*ROUTE, '1')
    new = (*ROUTE, '2')
    acquired = acquire(database, name, clock=clock, route=old)
    finish(database, name, acquired['token'], 'adapter_degraded', clock=clock, route=old)

    assert (
        acquire(database, name, clock=clock, allow_degraded=True, route=old)['status']
        == 'adapter_degraded'
    )
    assert acquire(database, name, clock=clock, route=new)['status'] == 'adapter_degraded'
    retry = acquire(database, name, clock=clock, allow_degraded=True, route=new)
    assert retry['status'] == 'acquired'
    assert (
        acquire(database, name, clock=clock, allow_degraded=True, route=new)['status'] == 'leased'
    )
    finish(database, name, retry['token'], 'adapter_degraded', clock=clock, route=new)
    assert route_health(database, name, *ROUTE)['adapter_revision'] == '2'
    assert (
        acquire(database, name, clock=clock, allow_degraded=True, route=new)['status']
        == 'adapter_degraded'
    )


def test_throttle_applies_across_routes_until_retry_after(tmp_path):
    database = tmp_path / 'state.db'
    clock = lambda: NOW  # noqa: E731
    name = 'wellfound'
    search = (*ROUTE, '1')
    detail = ('job_detail', 'public', '1')
    first = acquire(database, name, clock=clock, route=search)
    retry_at = NOW + timedelta(hours=2)
    finish(
        database, name, first['token'], 'throttled', retry_at=retry_at, clock=clock, route=search
    )
    blocked = acquire(database, name, clock=clock, route=detail)
    assert blocked['status'] == 'cooldown'
    assert blocked['eligible_at'] == retry_at.isoformat()
    assert acquire(database, name, clock=lambda: retry_at, route=detail)['status'] == 'acquired'
