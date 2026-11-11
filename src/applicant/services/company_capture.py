"""Explicit company profile capture and bridges from existing verified snapshots."""

from hashlib import sha256
from urllib.parse import urlsplit

from ..boards.public import structured_records
from ..domain.canonical import canonical_json, content_hash
from ..domain.company_evidence import CompanyResponse
from ..domain.contracts import now
from ..domain.providers import provider
from ..domain.research import Company, CompanyIdentifier, Culture, ResearchEvidence, ResearchImport
from ..errors import AuthFailed, Blocked, SourceError, Unparseable
from ..infra.http import HttpClient
from ..infra.store.company_evidence import CompanyEvidenceRepository
from ..infra.store.sqlite import Store
from .provider_health import acquire, finish
from .research import import_research, read_research

PROFILE_PROVIDERS = ('flexa', 'wellfound', 'builtin', 'welcometothejungle')


def capture_profile(database, selection, *, client=None, clock=now):
    if not isinstance(selection, dict) or set(selection) != {
        'entity_id',
        'provider',
        'url',
        'provider_company_id',
    }:
        raise ValueError(
            'profile capture requires entity_id, provider, url and provider_company_id'
        )
    name = provider(selection['provider']).id
    if name not in PROFILE_PROVIDERS:
        raise ValueError('unsupported public profile provider')
    root = (urlsplit(provider(name).url).hostname or '').removeprefix('www.')
    locator = urlsplit(selection['url'])
    if (
        locator.scheme != 'https'
        or (locator.hostname or '').removeprefix('www.') != root
        or locator.username
        or locator.password
    ):
        raise ValueError('profile locator must be on the selected provider')
    data = read_research(database)
    if not any(
        x.entity_id == selection['entity_id']
        and x.provider == name
        and x.provider_company_id == selection['provider_company_id']
        and x.verification_url == selection['url']
        for x in data['identifiers']
    ):
        raise ValueError(
            'capture requires an explicit verified company identifier and exact profile URL'
        )
    lease = acquire(database, name, clock=clock)
    if lease['status'] != 'acquired':
        return {'provider': name, **lease}
    http = HttpClient(name, client=client)
    try:
        response = http.get(selection['url'])
        if response.status_code == 403:
            raise Blocked('company profile access denied')
        if response.status_code >= 400 or len(response.content) > 8_000_000:
            raise Unparseable('profile unavailable or oversized; import evidence instead')
        records, _ = structured_records(response.text, 'Organization')
        matches = [
            x
            for x in records
            if x.get('url') == selection['url']
            or x.get('@id') == selection['url']
            or str(x.get('identifier')) == selection['provider_company_id']
        ]
        if len(matches) != 1 or not isinstance(matches[0].get('description'), str):
            raise Unparseable(
                'no uniquely identified public organization description; use research import'
            )
        stamp = clock()
        record = matches[0]
        item = ResearchEvidence(
            evidence_id=content_hash([selection, stamp, sha256(response.content).hexdigest()]),
            entity_id=selection['entity_id'],
            provider=name,
            source_url=selection['url'],
            acquired_at=stamp,
            scope='company',
            assertion='employer',
            excerpt=record['description'],
            payload=Culture(topic='company', description=record['description']),
            responses=(CompanyResponse.capture(selection['url'], response.content),),
        )
    except SourceError as error:
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
        finish(
            database,
            name,
            lease['token'],
            status,
            retry_at=getattr(error, 'retry_at', None),
            clock=clock,
        )
        raise
    finally:
        http.close()
    # Persistence failures are never provider failures.
    result = import_research(database, ResearchImport(evidence=(item,)))
    finish(database, name, lease['token'], 'success', clock=clock)
    return {
        **result,
        'evidence_id': item.evidence_id,
        'source_observation_date': 'unknown',
        'response_sha256': sha256(response.content).hexdigest(),
    }


def bridge_company_snapshot(database, snapshot_id, entity_id):
    data = read_research(database)
    with Store.open_readonly(database) as store, store.read_snapshot():
        repo = CompanyEvidenceRepository(store.db)
        snapshot = repo.snapshot(snapshot_id)
        association = repo.association(snapshot.association_id)
    if not any(
        x.entity_id == entity_id
        and x.provider == snapshot.provider
        and x.provider_company_id == snapshot.provider_company_id
        for x in data['identifiers']
    ):
        raise ValueError(
            'snapshot bridge requires a verified provider company identifier for the target entity'
        )
    excerpt = canonical_json(snapshot.payload)
    evidence = ResearchEvidence(
        evidence_id='company-snapshot-' + content_hash([snapshot.snapshot_id, entity_id]),
        entity_id=entity_id,
        provider=snapshot.provider,
        source_url=association.verification_url,
        acquired_at=snapshot.acquired_at,
        observed_at=snapshot.observed_at,
        scope='company',
        assertion='platform',
        excerpt=excerpt,
        payload=Culture(
            topic='company' if snapshot.provider in ('crunchbase', 'tracxn') else 'culture',
            description=excerpt,
        ),
        responses=snapshot.responses,
    )
    return import_research(database, ResearchImport(evidence=(evidence,)))


def backfill_verified_companies(database):
    """Only verified schema-7 associations supply identities; names are not merged."""
    with Store.open_readonly(database) as store, store.read_snapshot():
        rows = store.db.execute(
            'SELECT payload FROM company_associations ORDER BY association_id'
        ).fetchall()
    import json

    existing = read_research(database)
    companies, identifiers = {}, {}
    known_companies = {x.entity_id: x for x in existing['companies']}
    known_identifiers = {(x.provider, x.provider_company_id): x for x in existing['identifiers']}
    for (payload,) in rows:
        value = json.loads(payload)
        if (value['provider'], value['provider_company_id']) in known_identifiers:
            continue
        identity = value['provider'] + ':' + value['provider_company_id']
        key = 'verified-' + content_hash(identity)
        companies.setdefault(
            key,
            known_companies.get(key)
            or Company(
                entity_id=key,
                name=identity,
                recorded_at=value['verified_at'],
                source_url=value['verification_url'],
            ),
        )
        identifier_key = 'verified-id-' + content_hash(identity)
        identifiers.setdefault(
            identifier_key,
            CompanyIdentifier(
                identifier_id=identifier_key,
                entity_id=key,
                provider=value['provider'],
                provider_company_id=value['provider_company_id'],
                verified_at=value['verified_at'],
                verification_url=value['verification_url'],
            ),
        )
    return import_research(
        database,
        ResearchImport(
            companies=tuple(companies.values()), identifiers=tuple(identifiers.values())
        ),
    )
