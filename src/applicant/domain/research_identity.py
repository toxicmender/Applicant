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


def match_jobs(observations):
    groups = defaultdict(list)
    # One latest version per posting, with deterministic ties.
    latest = {}
    for observation in observations:
        key = (observation.job.source, observation.job.id or observation.source_url)
        if key not in latest or (observation.acquired_at, observation.job_version_id) > (
            latest[key].acquired_at,
            latest[key].job_version_id,
        ):
            latest[key] = observation
    for observation in latest.values():
        groups[identity_key(observation)].append(observation)
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
