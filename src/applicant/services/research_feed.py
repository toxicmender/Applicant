"""Collect bounded observations, isolate provider failures, and freeze a feed."""

from hashlib import sha256

from ..domain.canonical import canonical_json, content_hash
from ..domain.contracts import now
from ..domain.providers import provider
from ..domain.research import ListingObservation, ResearchImport
from ..domain.research_identity import match_jobs
from ..errors import AuthFailed, Blocked, SourceError, Unparseable
from ..infra.provider_registry import board_client
from .provider_health import acquire, finish
from .research import import_research, read_research, save_snapshot


def observations_from(client, jobs, stamp, name):
    observations = []
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
        observations.append(ListingObservation(job_version_id=content_hash(data), **data))
    return observations


def collect_feed(database, query, *, factory=board_client, clock=now, parent=None, probe=False):
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
    outcomes, acquired = [], []
    for name in names:
        if provider(name).board is None:
            outcomes.append(
                {
                    'provider': name,
                    'status': 'import_reference',
                    'command': 'applicant research import --from-file evidence.json',
                }
            )
            continue
        lease = acquire(database, name, clock=clock, allow_degraded=probe)
        if lease['status'] != 'acquired':
            outcomes.append({'provider': name, **lease})
            continue
        client = None
        jobs = []
        network_finished = False
        try:
            client = factory(name, headless=True)
            if query.get('urls', {}).get(name):
                if not callable(getattr(client, 'capture', None)):
                    raise ValueError('provider does not support explicit public job URLs')
                urls = query['urls'][name]
                if not isinstance(urls, list) or len(urls) > 25:
                    raise ValueError('capture accepts at most 25 job URLs per provider')
                for url in urls:
                    jobs.extend(client.capture(url))
                    if len(jobs) >= limit:
                        break
                jobs = jobs[:limit]
            else:
                jobs = client.search(
                    query.get('keywords', ''), location=query.get('location', ''), limit=limit
                )
            network_finished = True
            observations = observations_from(client, jobs, clock(), name)
        except (SourceError, OSError, ValueError) as error:
            status = (
                'needs_setup'
                if isinstance(error, AuthFailed)
                else 'adapter_degraded'
                if isinstance(error, Unparseable)
                else 'throttled'
                if 'rate limit' in str(error).lower()
                else 'blocked'
                if isinstance(error, Blocked)
                else 'unavailable'
            )
            partial = getattr(client, 'partial_jobs', [])
            partial = jobs or (partial if isinstance(partial, list) else [])
            count = 0
            if partial and not network_finished:
                observations = observations_from(client, partial[:limit], clock(), name)
                import_research(database, ResearchImport(observations=tuple(observations)))
                acquired.extend(observations)
                count = len(observations)
            finish(
                database,
                name,
                lease['token'],
                status,
                retry_at=getattr(error, 'retry_at', None),
                clock=clock,
            )
            outcomes.append(
                {
                    'provider': name,
                    'status': status,
                    'error_type': type(error).__name__,
                    'preserved_count': count,
                }
            )
            continue
        finally:
            if client:
                client.close()
        # Database errors are deliberately outside provider failure handling.
        import_research(database, ResearchImport(observations=tuple(observations)))
        finish(database, name, lease['token'], 'success', clock=clock)
        acquired.extend(observations)
        outcomes.append({'provider': name, 'status': 'success', 'count': len(observations)})
    value = {
        'kind': 'research_feed',
        'schema_version': 1,
        'created_at': clock().isoformat(),
        'query': query,
        'parent_snapshot_id': parent,
        'observations': [x.model_dump(mode='json') for x in acquired],
        'job_version_ids': [x.job_version_id for x in acquired],
        'groups': match_jobs(acquired),
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
