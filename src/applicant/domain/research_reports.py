"""A single, self-contained report document for JSON, Markdown and PDF."""

import base64
import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from pydantic import Field, model_validator

from .canonical import canonical_json
from .company_time import event_bounds, event_phase
from .contracts import Timestamp
from .providers import provider
from .research import Record
from .research_identity import match_jobs
from .research_time import freshness, seasonal_schedules, zone


class ReportSection(Record):
    id: str
    title: str
    columns: tuple[str, ...] = Field(min_length=1, max_length=8)
    rows: tuple[tuple[str, ...], ...]
    total_rows: int | None = None
    note: str = ''

    @model_validator(mode='after')
    def widths(self):
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError('report row width differs from its column count')
        return self


class ReportDocument(Record):
    kind: Literal['applicant_report'] = 'applicant_report'
    title: str
    created_at: Timestamp
    template_version: Literal['report-1'] = 'report-1'
    as_of: str | None = None
    sections: tuple[ReportSection, ...]
    manifest: dict
    snapshot_id: str | None = None


SECTIONS = (
    'jobs',
    'compensation',
    'culture',
    'policy',
    'company',
    'technologies',
    'inherited-context',
)


def summary_text(payload):
    values = payload.model_dump(mode='json')
    return '; '.join(
        f'{key.replace("_", " ")}: {value}'
        for key, value in values.items()
        if key not in ('schema_version', 'kind', 'schedule')
        and value is not None
        and value != []
        and value != ()
    )


def company_report(data, selection, *, created_at):
    allowed = {
        'entity_ids',
        'evidence_ids',
        'job_version_ids',
        'as_of',
        'timezone',
        'sections',
        'summary',
        'freshness',
    }
    if not isinstance(selection, dict) or set(selection) - allowed:
        raise ValueError('invalid company report selection fields')
    entities = set(selection.get('entity_ids', []))
    chosen_ids = set(selection.get('evidence_ids', []))
    versions = set(selection.get('job_version_ids', []))
    if not entities and not versions and not chosen_ids:
        raise ValueError('select company entities, job versions or evidence IDs')
    tz = selection.get('timezone', 'UTC')
    applicant_zone = zone(tz)
    as_of = date.fromisoformat(
        selection.get('as_of', created_at.astimezone(applicant_zone).date().isoformat())
    )
    if as_of > created_at.astimezone(applicant_zone).date():
        raise ValueError('report date cannot be in the future')
    cutoff = min(
        datetime.combine(
            as_of + timedelta(days=1), datetime.min.time(), tzinfo=applicant_zone
        ).astimezone(timezone.utc),
        created_at,
    )
    sections = selection.get('sections', list(SECTIONS))
    if set(sections) - set(SECTIONS):
        raise ValueError('unknown report section')
    known_entities = {x.entity_id: x for x in data['companies'] if x.recorded_at < cutoff}
    if entities - known_entities.keys():
        raise ValueError('unknown company selection at report cutoff')
    observations = [
        x
        for x in data['observations']
        if x.acquired_at < cutoff
        and (x.employer_entity_id in entities or x.job_version_id in versions)
    ]
    if versions - {x.job_version_id for x in observations}:
        raise ValueError('unknown job version selection at report cutoff')
    versions.update(x.job_version_id for x in observations)
    all_evidence = [
        x
        for x in data['evidence']
        if x.acquired_at < cutoff and (x.observed_at is None or x.observed_at < cutoff)
    ]
    if chosen_ids - {x.evidence_id for x in all_evidence}:
        raise ValueError('unknown evidence selection at report cutoff')
    events = [x for x in data['events'] if x.recorded_at < cutoff]
    superseded = {x.supersedes for x in events if x.supersedes}
    events = [
        x
        for x in events
        if x.event_id not in superseded
        and ((start := event_bounds(x)[0]) is None or start < cutoff)
    ]
    relevant_events = [
        x
        for x in events
        if x.subject_id in entities or (x.event_type == 'merger' and x.related_id in entities)
    ]
    predecessor_ids = set(entities)
    while True:
        predecessors = {
            x.subject_id
            for x in events
            if x.event_type == 'merger' and x.related_id in predecessor_ids
        }
        if predecessors <= predecessor_ids:
            break
        predecessor_ids.update(predecessors)
    relevant_events = [
        x
        for x in events
        if x in relevant_events
        or (
            x.event_type == 'merger'
            and x.subject_id in predecessor_ids
            and x.related_id in predecessor_ids
        )
    ]
    historical_entities = {x.subject_id for x in relevant_events}
    evidence = [
        x
        for x in all_evidence
        if x.entity_id in entities | historical_entities
        or x.job_version_id in versions
        or x.evidence_id in chosen_ids
    ]
    rows = {name: [] for name in SECTIONS}
    for company in known_entities.values():
        if company.entity_id in entities:
            active_names = [
                x
                for x in data['names']
                if x.entity_id == company.entity_id
                and x.name_type in ('legal', 'trading')
                and x.recorded_at < cutoff
                and (x.effective_from is None or x.effective_from <= as_of)
                and (x.effective_to is None or as_of < x.effective_to)
            ]
            active_names.sort(
                key=lambda x: (x.effective_from or date.min, x.recorded_at, x.name_id), reverse=True
            )
            display_name = active_names[0].name if active_names else company.name
            rows['company'].append(
                [company.entity_id, 'Company identity', display_name, company.source_url]
            )
    job_groups = match_jobs(observations)
    by_version = {x.job_version_id: x for x in observations}
    for group in job_groups:
        observation = by_version[group['job_version_ids'][0]]
        age = (as_of - (observation.observed_at or observation.acquired_at).date()).days
        rows['jobs'].append(
            [
                observation.job.title,
                observation.job.company or 'Unknown employer',
                f'{group["status"]}; {len(group["sources"])} board(s); {observation.origin}; {observation.status}; '
                + (
                    'stale'
                    if age > selection.get('freshness', {}).get('jobs', 14)
                    else 'source date unknown'
                    if observation.observed_at is None
                    else 'current'
                ),
                '; '.join(
                    dict.fromkeys(by_version[key].source_url for key in group['job_version_ids'])
                ),
            ]
        )
    conversions = []
    for item in evidence:
        historical = [
            event
            for event in relevant_events
            if event.subject_id == item.entity_id
            and event.event_type != 'rename'
            and (
                item.entity_id not in entities
                or event_phase(event, item.observed_at, cutoff) != 'post-event contextual'
            )
        ]
        if historical:
            labels = []
            for event in historical:
                phase = event_phase(event, item.observed_at, cutoff)
                parent = known_entities.get(event.related_id)
                label = (
                    f'Historical evidence prior to {event.effective_date} acquisition by {parent.name if parent else event.related_id}.'
                    if phase == 'historical'
                    and event.event_type == 'acquisition'
                    and event.effective_date
                    else f'{phase.capitalize()} for {event.event_type}; effective {getattr(event, "effective_at", None) or event.effective_date or "unknown"}; related entity {parent.name if parent else event.related_id}.'
                )
                original = known_entities.get(item.entity_id)
                label += f' Original entity: {original.name if original else item.entity_id}; observed: {item.observed_at.date().isoformat() if item.observed_at else "unknown"}.'
                labels.append(label)
            rows['inherited-context'].append(
                [
                    item.payload.kind.title(),
                    ' | '.join(dict.fromkeys(labels)),
                    summary_text(item.payload),
                    item.source_url,
                ]
            )
            continue
        section = {
            'technology': 'technologies',
            'culture': 'culture',
            'policy': 'policy',
            'compensation': 'compensation',
        }[item.payload.kind]
        if item.payload.kind == 'culture' and item.payload.topic == 'company':
            section = 'company'
        detail = summary_text(item.payload)
        if item.payload.kind == 'policy' and item.payload.schedule:
            try:
                converted = seasonal_schedules(item.payload.schedule, tz, as_of)
                conversions.append({'evidence_id': item.evidence_id, 'variants': converted})
                detail += (
                    '; Employer hours: '
                    + item.payload.schedule.start
                    + '-'
                    + item.payload.schedule.end
                    + ' '
                    + item.payload.schedule.employer_timezone
                )
                for variant in converted:
                    detail += f'; {tz} from {variant["date"]}: {variant["local_start"]} to {variant["local_end"]}'
            except ValueError as error:
                detail += '; timezone unresolved: ' + str(error)
        try:
            source_name = provider(item.provider).name
        except ValueError:
            source_name = item.provider
        display = (
            item.payload.technology
            if item.payload.kind == 'technology'
            else item.payload.kind.title()
        )
        rows[section].append(
            [
                display,
                f'{source_name}; {item.assertion}; {item.scope}; {freshness(item, as_of, selection.get("freshness"))}',
                detail,
                item.source_url,
            ]
        )
    titles = {'inherited-context': 'Inherited Context'}
    output = []
    for name in sections:
        values = rows[name]
        if name == 'inherited-context' and not values:
            continue
        output.append(
            {
                'id': name,
                'title': titles.get(name, name.title()),
                'columns': ['Item', 'Context', 'Details', 'Source'],
                'rows': values[:5] if selection.get('summary') else values,
                'total_rows': len(values),
                'note': 'No selected evidence available.' if not values else '',
            }
        )
    return {
        'kind': 'applicant_report',
        'schema_version': 1,
        'title': 'Job and Company Research',
        'created_at': created_at.isoformat(),
        'as_of': as_of.isoformat(),
        'template_version': 'report-1',
        'sections': output,
        'manifest': {
            'cutoff': cutoff.isoformat(),
            'corporate_time_policy': 'event-precision-2',
            'selection': selection,
            'job_version_ids': sorted(versions),
            'job_groups': job_groups,
            'evidence_ids': [x.evidence_id for x in evidence],
            'corporate_event_ids': [x.event_id for x in relevant_events],
            'conversions': conversions,
            'companies': [known_entities[x].model_dump(mode='json') for x in sorted(entities)],
            'events': [x.model_dump(mode='json') for x in relevant_events],
            'names': [
                x.model_dump(mode='json')
                for x in data['names']
                if x.entity_id in entities and x.recorded_at < cutoff
            ],
            'evidence': [x.model_dump(mode='json') for x in evidence],
        },
    }


def snapshot_report(snapshot, *, limit=25, offset=0):
    if snapshot['kind'] == 'applicant_report':
        return snapshot
    if snapshot['kind'] == 'research_feed':
        versions = {x['job_version_id']: x for x in snapshot['observations']}
        rows = []
        for group in snapshot['groups'][offset : offset + limit]:
            postings = [versions[x] for x in group['job_version_ids']]
            rows.append(
                [
                    postings[0]['job']['title'],
                    group['status'],
                    ', '.join(group['sources']),
                    '; '.join(x['source_url'] for x in postings),
                ]
            )
        section = {
            'id': 'jobs',
            'title': 'Cross-checked Jobs',
            'columns': ['Role', 'Identity', 'Boards', 'Sources'],
            'rows': rows,
        }
    elif snapshot['kind'] == 'technology_analytics':
        rows = [
            [
                x.get('technology', x.get('stack')),
                str(x['observed_openings']),
                canonical_json(x['origins']),
                canonical_json(x['compensation']),
            ]
            for x in snapshot['rows'][offset : offset + limit]
        ]
        section = {
            'id': 'technologies',
            'title': 'Technology Analytics',
            'columns': ['Technology / stack', 'Observed openings', 'Origins', 'Compensation'],
            'rows': rows,
        }
    else:
        raise ValueError('unsupported report snapshot kind')
    return {
        'kind': 'applicant_report',
        'schema_version': 1,
        'title': section['title'],
        'created_at': snapshot['created_at'],
        'template_version': 'report-1',
        'sections': [section],
        'manifest': {'snapshot': snapshot, 'offset': offset, 'limit': limit},
    }


def markdown_body(document):
    def cell(value):
        return (
            str(value)
            .replace('\\', '\\\\')
            .replace('|', '\\|')
            .replace('\n', '<br>')
            .replace('\r', '')
        )

    lines = ['# ' + document['title'], '', 'Generated: ' + document['created_at'], '']
    for section in document['sections']:
        lines.extend(['## ' + section['title'], ''])
        if section.get('note'):
            lines.extend([section['note'], ''])
        lines.append('| ' + ' | '.join(cell(x) for x in section['columns']) + ' |')
        lines.append('| ' + ' | '.join('---' for _ in section['columns']) + ' |')
        for row in section['rows']:
            lines.append('| ' + ' | '.join(cell(x) for x in row) + ' |')
        lines.append('')
    return '\n'.join(lines)


def render_markdown(document):
    body = markdown_body(document)
    metadata = base64.b64encode(canonical_json(document).encode('utf-8')).decode('ascii')
    return body + '\n<!-- applicant-report-v1:' + metadata + ' -->\n'


def parse_markdown(text):
    match = re.search(r'\n<!-- applicant-report-v1:([A-Za-z0-9+/=]+) -->\s*$', text)
    if not match:
        raise ValueError(
            'expected Applicant Markdown with report-v1 metadata; export with --format markdown'
        )
    value = json.loads(base64.b64decode(match[1], validate=True))
    ReportDocument.model_validate(value)
    if text[: match.start()] != markdown_body(value):
        raise ValueError(
            'Markdown content differs from its report metadata; edit the report JSON and export again'
        )
    return value
