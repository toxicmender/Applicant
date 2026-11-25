"""Collect bounded observations, isolate provider failures, and freeze a feed."""

from hashlib import sha256

from ..domain.canonical import canonical_json, content_hash
from ..domain.contracts import now
from ..domain.providers import provider
from ..domain.research import ListingObservation, ResearchImport, posting_languages
from ..domain.research_identity import match_jobs
from ..domain.source_operations import eligible_route
from ..errors import AuthFailed, Blocked, SourceError, Unparseable
from ..infra.board_worker import OwnedBoard
from ..infra.owned_worker import CleanupUnconfirmed, WorkerFailure
from ..infra.provider_registry import board_client
from .provider_health import Acquisition, LeaseFailure
from .research import import_research, read_research, save_snapshot
from .source_operations import operations


def observations_from(client, jobs, stamp, name):
    observations = []
    invalid = None
    for job in jobs:
        data = {
            'job': job.model_dump(),
            'acquired_at': stamp,
            'source_url': getattr(client, 'source_urls', {}).get(job.id)
            or job.url
            or provider(name).url,
            'content_sha256': sha256(canonical_json(job).encode()).hexdigest(),
            'status': 'open',
            'origin': 'unknown',
        }
        data.update(getattr(client, 'requisition_metadata', {}).get(job.id, {}))
        raw = getattr(client, 'raw', None)
        body = raw.get(data['source_url']) if isinstance(raw, dict) else raw
        if isinstance(body, bytes) and body:
            data['content_sha256'] = sha256(body).hexdigest()
            data['content_representation'] = 'http_response'
        try:
            observations.append(ListingObservation(job_version_id=content_hash(data), **data))
        except ValueError as error:
            invalid = error
    return observations, invalid


def collect_feed(
    database,
    query,
    *,
    factory=board_client,
    clock=now,
    parent=None,
    probe=False,
    settings=None,
    use_session=True,
    selected_route=None,
):
    allowed = {'providers', 'keywords', 'location', 'limit', 'require_all_sources', 'urls'}
    if not isinstance(query, dict) or set(query) - allowed or not query.get('providers'):
        raise ValueError(
            'feed query requires providers and accepts keywords, location, limit, require_all_sources'
        )
    if not isinstance(query['providers'], list) or not all(
        isinstance(x, str) for x in query['providers']
    ):
        raise ValueError('providers must be a list of provider IDs')
    if any(not isinstance(query.get(key, ''), str) for key in ('keywords', 'location')):
        raise ValueError('keywords and location must be strings')
    if not isinstance(query.get('urls', {}), dict) or any(
        not isinstance(value, list)
        or not all(isinstance(url, str) for url in value)
        or len(value) > 25
        for value in query.get('urls', {}).values()
    ):
        raise ValueError('urls must map providers to at most 25 URL strings')
    if type(query.get('require_all_sources', False)) is not bool:
        raise ValueError('require_all_sources must be boolean')
    names = [provider(x).id for x in query['providers']]
    if len(names) != len(set(names)):
        raise ValueError('feed providers must be unique')
    limit = query.get('limit', 25)
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError('feed limit must be between 1 and 500')
    for name, urls in query.get('urls', {}).items():
        if name not in names:
            raise ValueError('URL provider must be a canonical selected provider')
        if urls and not (provider(name).board or '').startswith('applicant.boards.public.'):
            raise ValueError('provider does not support explicit public job URLs')
    outcomes, acquired, committed = [], [], []
    for index, name in enumerate(names):
        if provider(name).board is None:
            outcomes.append(
                {
                    'provider': name,
                    'status': 'import_reference',
                    'command': 'applicant research import --from-file evidence.json',
                }
            )
            continue
        active_route = selected_route
        managed = use_session
        if active_route is None and factory is board_client:
            key = 'job_detail' if query.get('urls', {}).get(name) else 'job_search'
            configured = operations(provider(name)).get(key)
            if configured is None:
                outcomes.append({'provider': name, 'status': 'route_unavailable'})
                continue
            routes = configured.routes
            if any(route.credential_kind == 'browser_session' for route in routes):
                from .provider_setup import provider_session

                record = (
                    provider_session(settings, name).snapshot() if settings and use_session else {}
                )
                managed = bool(record) and record.get('status') not in ('cleared', 'needs_setup')
                if not managed:
                    routes = tuple(
                        route for route in routes if route.credential_kind != 'browser_session'
                    )
            chosen, exclusions = eligible_route(
                configured.model_copy(update={'routes': routes}), 'persist'
            )
            if chosen is None:
                outcomes.append(
                    {
                        'provider': name,
                        'status': next(iter(exclusions.values()), 'route_unavailable'),
                        'command': 'applicant research import --from-file evidence.json',
                    }
                )
                continue
            active_route = (key, chosen.id, chosen.adapter_revision)
        with Acquisition(database, name, clock=clock, probe=probe, route=active_route) as owner:
            lease = owner.result
            if lease['status'] != 'acquired':
                outcomes.append({'provider': name, **lease})
                continue
            client = None
            jobs = []
            status, error, retry_at = 'success', None, None
            try:
                client = (
                    OwnedBoard(name, owner.check, settings=settings if managed else None)
                    if factory is board_client
                    else factory(name, headless=True)
                )
                if query.get('urls', {}).get(name):
                    if not callable(getattr(client, 'capture', None)):
                        raise Unparseable('provider capture adapter is missing')
                    urls = query['urls'][name]
                    for url in urls:
                        owner.check()
                        jobs.extend(client.capture(url))
                        if len(jobs) >= limit:
                            break
                    jobs = jobs[:limit]
                else:
                    jobs = client.search(
                        query.get('keywords', ''), location=query.get('location', ''), limit=limit
                    )
                owner.check()
            except CleanupUnconfirmed as failure:
                status, error = 'cleanup_unconfirmed', failure
            except (LeaseFailure, WorkerFailure) as failure:
                status, error = 'infrastructure_failure', failure
            except (SourceError, OSError, ValueError) as failure:
                error = failure
                retry_at = getattr(failure, 'retry_at', None)
                status = (
                    'needs_setup'
                    if isinstance(failure, AuthFailed)
                    else 'adapter_degraded'
                    if isinstance(failure, Unparseable)
                    else 'throttled'
                    if 'rate limit' in str(failure).lower()
                    else 'blocked'
                    if isinstance(failure, Blocked)
                    else 'unavailable'
                )
            finally:
                if client is not None:
                    try:
                        client.close()
                    except CleanupUnconfirmed as failure:
                        status, error = 'cleanup_unconfirmed', failure
                    except Exception as failure:  # noqa: BLE001 - adapter teardown boundary
                        if status != 'cleanup_unconfirmed':
                            status, error = 'infrastructure_failure', failure
            if status == 'cleanup_unconfirmed':
                return {
                    'kind': 'research_feed',
                    'schema_version': 1,
                    'outcome': 'partial_commit' if committed else 'aborted',
                    'committed_providers': committed,
                    'failed_provider': name,
                    'skipped_providers': names[index + 1 :],
                    'query': query,
                    'job_version_ids': [item.job_version_id for item in acquired],
                    'outcomes': [*outcomes, {'provider': name, 'status': status}],
                }
            partial = getattr(client, 'partial_jobs', [])
            if error is not None and isinstance(partial, list):
                jobs = [*jobs, *partial]
            observations, invalid = observations_from(client, jobs[:limit], clock(), name)
            if invalid is not None and status != 'infrastructure_failure':
                status, error = 'adapter_degraded', invalid
            # Database errors are deliberately outside provider failure handling.
            import_research(
                database,
                ResearchImport(
                    observations=tuple(observations),
                    evidence=tuple(
                        fact
                        for item in observations
                        for fact in posting_languages(
                            item.job,
                            item.job_version_id,
                            item.acquired_at,
                            item.source_url,
                        )
                    ),
                ),
            )
            acquired.extend(observations)
            if observations:
                committed.append(name)
            eligible_at = None
            if status != 'infrastructure_failure':
                try:
                    eligible_at = owner.finish(status, retry_at=retry_at).get('eligible_at')
                except LeaseFailure as failure:
                    status, error = 'infrastructure_failure', failure
            outcome: dict[str, str | int] = {'provider': name, 'status': status}
            if eligible_at:
                outcome['eligible_at'] = eligible_at
            if isinstance(client, OwnedBoard):
                outcome.update(client.access)
            if error is None:
                outcome['count'] = len(observations)
            else:
                outcome.update(error_type=type(error).__name__, preserved_count=len(observations))
                if status == 'infrastructure_failure':
                    outcome['retryable'] = True
            outcomes.append(outcome)
    value = {
        'kind': 'research_feed',
        'schema_version': 1,
        'created_at': clock().isoformat(),
        'query': query,
        'parent_snapshot_id': parent,
        'observations': [x.model_dump(mode='json') for x in acquired],
        'job_version_ids': [x.job_version_id for x in acquired],
        'groups': match_jobs(
            acquired, evidence=read_research(database)['observations'] if acquired else ()
        ),
        'outcomes': outcomes,
    }
    return save_snapshot(database, value)


def feed_from_imports(database, *, clock=now):
    observations = read_research(database)['observations']
    return save_snapshot(
        database,
        {
            'kind': 'research_feed',
            'schema_version': 1,
            'created_at': clock().isoformat(),
            'query': {'providers': ['import']},
            'observations': [x.model_dump(mode='json') for x in observations],
            'job_version_ids': [x.job_version_id for x in observations],
            'groups': match_jobs(observations),
            'outcomes': [{'provider': 'import', 'status': 'imported'}],
        },
    )
