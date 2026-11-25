"""Conservative requisition matching, with source lineage kept explicit."""

from collections import defaultdict
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .canonical import content_hash

TRACKING = {
    'utm_source',
    'utm_medium',
    'utm_campaign',
    'utm_content',
    'utm_term',
    'gh_src',
    'source',
    'referrer',
}


def requisition_url(url):
    parsed = urlsplit(url)
    query = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k not in TRACKING
    ]
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip('/'),
            urlencode(sorted(query)),
            '',
        )
    )


def identity_key(observation):
    if observation.employer_requisition_url:
        return 'url:' + requisition_url(observation.employer_requisition_url)
    if observation.employer_entity_id and observation.employer_requisition_id:
        return 'req:' + content_hash(
            [observation.employer_entity_id, observation.employer_requisition_id]
        )
    return 'board:' + content_hash(
        [observation.job.source, observation.job.id or observation.source_url]
    )


def match_jobs(observations, *, evidence=()):
    groups = defaultdict(list)
    # One latest version per posting, with deterministic ties.
    latest = {}
    known_requisitions = defaultdict(dict)
    observations = tuple(observations)
    for observation in observations:
        key = (observation.job.source, observation.job.id or observation.source_url)
        if key not in latest or (observation.acquired_at, observation.job_version_id) > (
            latest[key].acquired_at,
            latest[key].job_version_id,
        ):
            latest[key] = observation
    for observation in (*observations, *evidence):
        key = (observation.job.source, observation.job.id or observation.source_url)
        if key not in latest:
            continue
        identity = identity_key(observation)
        if not identity.startswith('board:'):
            exact = (*key, requisition_url(observation.source_url))
            previous = known_requisitions[exact].get(identity)
            if previous is None or (observation.acquired_at, observation.job_version_id) > (
                previous.acquired_at,
                previous.job_version_id,
            ):
                known_requisitions[exact][identity] = observation
    inherited = {}
    for key, observation in latest.items():
        identity = identity_key(observation)
        exact = (*key, requisition_url(observation.source_url))
        known = known_requisitions.get(exact, {})
        if identity.startswith('board:') and len(known) == 1:
            confirmed_identity, evidence = next(iter(known.items()))
            if (
                not observation.employer_entity_id
                or observation.employer_entity_id == evidence.employer_entity_id
            ) and (
                not observation.employer_requisition_id
                or observation.employer_requisition_id == evidence.employer_requisition_id
            ):
                identity = confirmed_identity
                inherited[observation.job_version_id] = evidence.job_version_id
        groups[identity].append(observation)
    output = []
    for identity, values in sorted(groups.items()):
        values.sort(key=lambda x: (x.job.source, x.job_version_id))
        sources = sorted({x.job.source for x in values})
        employers = {x.employer_entity_id for x in values if x.employer_entity_id}
        conflict = len(employers) > 1
        confirmed = len(sources) > 1 and not identity.startswith('board:') and not conflict
        origins = sorted({x.syndication_origin for x in values if x.syndication_origin})
        differences = {}
        for field in ('title', 'company', 'location', 'salary', 'remote'):
            distinct = sorted(
                {str(getattr(x.job, field)) for x in values if getattr(x.job, field) is not None}
            )
            if len(distinct) > 1:
                differences[field] = distinct
        output.append(
            {
                'job_group_id': content_hash(identity),
                'match_revision': content_hash(
                    {
                        'identity': identity,
                        'versions': [x.job_version_id for x in values],
                        'conflict': conflict,
                        'policy': 'requisition-1',
                    }
                ),
                'match_policy_version': 'requisition-1',
                'status': 'confirmed' if confirmed else 'single_source',
                'employer_conflict': conflict,
                'job_version_ids': [x.job_version_id for x in values],
                'sources': sources,
                'syndication_origins': origins,
                'independent_corroboration': False,
                'direct_sources': sorted({x.job.source for x in values if x.origin == 'direct'}),
                'conflicts': differences,
                'identity_basis': identity,
                'possible_match_groups': [],
            }
        )
        lineage = {
            x.job_version_id: inherited[x.job_version_id]
            for x in values
            if x.job_version_id in inherited
        }
        if lineage:
            output[-1]['identity_evidence_ids'] = lineage
    for i, group in enumerate(output):
        sample = groups[group['identity_basis']][0]
        signature = tuple(
            (v or '').casefold().strip()
            for v in (sample.job.company, sample.job.title, sample.job.location)
        )
        if not signature[0] or not signature[1]:
            continue
        for other in output[i + 1 :]:
            rival = groups[other['identity_basis']][0]
            if (
                sample.employer_entity_id
                and sample.employer_entity_id == rival.employer_entity_id
                and sample.employer_requisition_id
                and rival.employer_requisition_id
                and sample.employer_requisition_id != rival.employer_requisition_id
            ):
                continue
            other_signature = tuple(
                (v or '').casefold().strip()
                for v in (rival.job.company, rival.job.title, rival.job.location)
            )
            if signature == other_signature:
                group['possible_match_groups'].append(other['job_group_id'])
                other['possible_match_groups'].append(group['job_group_id'])
    for group in output:
        if group['status'] != 'confirmed' and group['possible_match_groups']:
            group['status'] = 'possible_match'
        group['match_revision'] = content_hash(
            {key: value for key, value in group.items() if key != 'match_revision'}
        )
    return output
