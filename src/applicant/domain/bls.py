"""OEWS code tables, bounded series selection and source-defined annual wages."""

import json
import math
import re
from datetime import date
from decimal import Decimal
from itertools import product

from .benchmarks import BenchmarkCohort, BenchmarkObservation, BenchmarkQuery, BenchmarkResponse
from .canonical import content_hash

MAX_BYTES = 4_000_000
MAX_SERIES = 25
TABLES = {
    'area': ('state_code', 'area_code', 'areatype_code', 'area_name'),
    'occupation': ('occupation_code', 'occupation_name', 'selectable'),
    'industry': ('industry_code', 'industry_name', 'selectable'),
    'datatype': ('datatype_code', 'datatype_name'),
}
STATISTICS = {'mean': '04', 'p10': '11', 'p25': '12', 'median': '13', 'p75': '14', 'p90': '15'}


def parse_table(raw: bytes, name: str) -> dict[str, dict]:
    if len(raw) > MAX_BYTES:
        raise ValueError('BLS code table exceeds byte limit')
    # BLS mapping files use literal tabs; keep decoding independent of I/O APIs.
    reader = (line.split('\t') for line in raw.decode('utf-8-sig').splitlines())
    header = [s.strip() for s in next(reader, [])]
    if len(set(header)) != len(header) or not set(TABLES[name]) <= set(header):
        raise ValueError('invalid BLS code table header')
    result = {}
    for fields in reader:
        if not any(v.strip() for v in fields):
            continue
        if len(fields) != len(header):
            raise ValueError('invalid BLS code table row')
        row = dict(zip(header, (v.strip() for v in fields), strict=True))
        code = row[f'{name}_code']
        pattern = {
            'area': r'[0-9]{7}',
            'occupation': r'[0-9]{6}',
            'industry': r'[0-9][0-9-]{5}',
            'datatype': r'[0-9]{2}',
        }[name]
        if not re.fullmatch(pattern, code) or not row[f'{name}_name']:
            raise ValueError('invalid BLS code or label')
        if name == 'area':
            if not re.fullmatch(r'[A-Z]', row['areatype_code']) or not re.fullmatch(
                r'[0-9]{2}', row['state_code']
            ):
                raise ValueError('invalid BLS area classification')
            code = row['areatype_code'] + ':' + code
        elif name in ('occupation', 'industry') and row['selectable'] not in ('T', 'F'):
            raise ValueError('invalid BLS selectable marker')
        if code in result:
            raise ValueError('duplicate BLS code')
        result[code] = row
    if not result:
        raise ValueError('empty BLS code table')
    return result


def validate_query(query: BenchmarkQuery, *, as_of: date) -> BenchmarkQuery:
    if query.provider != 'bls' or query.dataset != 'oews':
        raise ValueError('BLS supports only oews')
    if not {'area', 'occupation', 'year'} <= query.filters.keys() or not query.filters.keys() <= {
        'area',
        'occupation',
        'year',
        'industry',
        'statistic',
    }:
        raise ValueError(
            'select explicit area, occupation and year; unknown filters are unsupported'
        )
    filters = {'industry': ('000000',), 'statistic': ('median',)} | query.filters
    years = filters['year']
    if (
        len(years) != 1
        or not re.fullmatch(r'[0-9]{4}', years[0])
        or not 2021 <= int(years[0]) <= as_of.year
    ):
        raise ValueError('select one reference year from 2021 through the current year')
    for key, pattern in (
        ('area', r'[A-Z]:[0-9]{7}'),
        ('occupation', r'[0-9]{6}'),
        ('industry', r'[0-9][0-9-]{5}'),
    ):
        if any(not re.fullmatch(pattern, v) for v in filters[key]):
            raise ValueError(f'invalid BLS {key} code format')
    if not set(filters['statistic']) <= STATISTICS.keys():
        raise ValueError('select annual mean, median, p10, p25, p75 or p90')
    if (
        math.prod(len(filters[k]) for k in ('area', 'occupation', 'industry', 'statistic'))
        > MAX_SERIES
    ):
        raise ValueError('BLS query exceeds 25 series; split into explicit queries')
    return BenchmarkQuery(provider='bls', dataset='oews', filters=filters)


def series_plan(query: BenchmarkQuery, tables: dict) -> dict[str, dict]:
    for name in ('area', 'occupation', 'industry'):
        for code in query.filters[name]:
            if code not in tables[name] or tables[name][code].get('selectable', 'T') != 'T':
                raise ValueError(f'unknown or unselectable BLS {name}; use benchmark discover')
    result = {}
    for area, occupation, industry, statistic in product(
        *(query.filters[k] for k in ('area', 'occupation', 'industry', 'statistic'))
    ):
        datatype = STATISTICS[statistic]
        label = tables['datatype'].get(datatype, {}).get('datatype_name', '').lower()
        marker = (
            'mean' if statistic == 'mean' else 'median' if statistic == 'median' else statistic[1:]
        )
        if 'annual' not in label or marker not in label:
            raise ValueError('BLS datatype mapping changed')
        series_id = 'OEU' + area.replace(':', '') + industry + occupation + datatype
        result[series_id] = {
            'area': area,
            'occupation': occupation,
            'industry': industry,
            'statistic': statistic,
        }
    return result


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate BLS JSON property')
        result[key] = value
    return result


def _constant(value):
    raise ValueError(f'nonfinite BLS JSON number: {value}')


def parse_observations(raw: BenchmarkResponse, query: BenchmarkQuery, plan: dict):
    if len(raw.body) > MAX_BYTES:
        raise ValueError('BLS API response exceeds byte limit')
    payload = json.loads(
        raw.body.decode('utf-8'),
        parse_float=Decimal,
        parse_constant=_constant,
        object_pairs_hook=_object,
    )
    if (
        not isinstance(payload, dict)
        or payload.get('status') != 'REQUEST_SUCCEEDED'
        or payload.get('message', []) != []
    ):
        raise ValueError('BLS API returned failure, warning or incomplete query')
    results = payload.get('Results')
    if isinstance(results, list) and len(results) == 1:
        results = results[0]
    series = results.get('series') if isinstance(results, dict) else None
    if not isinstance(series, list) or len(series) != len(plan):
        raise ValueError('BLS response does not contain every requested series')
    observations = {}
    year = query.filters['year'][0]
    for entry in series:
        if (
            not isinstance(entry, dict)
            or entry.get('seriesID') not in plan
            or entry['seriesID'] in observations
        ):
            raise ValueError('unexpected or duplicate BLS series')
        series_id = entry['seriesID']
        rows = entry.get('data')
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise ValueError('BLS selected annual observation is missing or duplicated')
        row = rows[0]
        if row.get('year') != year or row.get('period') != 'A01':
            raise ValueError('BLS returned a different year or period')
        footnotes = row.get('footnotes', [])
        if not isinstance(footnotes, list):
            raise ValueError('invalid BLS footnotes')
        flags, notes = [], []
        for footnote in footnotes:
            if not isinstance(footnote, dict) or any(
                not isinstance(v, str) for v in footnote.values()
            ):
                raise ValueError('invalid BLS footnote')
            if footnote.get('code'):
                flags.append(footnote['code'])
            if footnote.get('text'):
                notes.append(footnote['text'])
        value = row.get('value')
        if not isinstance(value, str):
            raise ValueError('BLS wage must be source decimal text')
        value = value.strip()
        amount = None
        status = 'available'
        text = ' '.join(notes).lower()
        if value == '*' or 'not released' in text or 'suppressed' in text:
            status = 'suppressed'
        elif value in ('-', '...'):
            status = 'unavailable'
        elif value == '#':
            status = 'censored'
        else:
            if not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?\+?', value):
                raise ValueError('invalid BLS wage value')
            amount = Decimal(value.rstrip('+'))
            if value.endswith('+') or any(
                term in text
                for term in ('equal to or greater than', 'greater than or equal to', 'top-coded')
            ):
                status = 'censored'
        selected = plan[series_id]
        area_type, area = selected['area'].split(':')
        cohort = BenchmarkCohort(
            geography_system='BLS-OEWS-area',
            geography_code=area,
            occupation_system='SOC-2018',
            occupation_code=selected['occupation'],
            population='nonfarm-wage-and-salary-employees',
            industry_system='BLS-OEWS-industry',
            industry_code=selected['industry'],
            working_time='full-and-part-time-employees',
            reference_start=date(int(year), 5, 1),
            reference_end=date(int(year), 5, 31),
            methodology='BLS-OEWS-MB3-SOC2018-v1',
            wage_definition='oews_straight_time_gross_wages_including_incentive_pay',
            basis='gross',
            statistic=selected['statistic'],
            dimensions={'area_type': area_type, 'period': 'A01'},
        )
        observations[series_id] = BenchmarkObservation(
            observation_id=content_hash({'series': series_id, 'year': year, 'period': 'A01'}),
            response_id=raw.response_id,
            cohort=cohort,
            amount=amount,
            currency='USD',
            period='annual',
            status=status,
            flags=tuple(flags),
            notes=(
                *notes,
                'Annual wage rate at the May reference period, not realized annual income.',
            ),
        )
    return tuple(observations[k] for k in plan)
