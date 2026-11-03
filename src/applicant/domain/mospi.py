"""PLFS salaried earnings: discovered cohorts and bounded, complete pagination."""

import json
import math
import re
from datetime import date
from decimal import Decimal

from .benchmarks import BenchmarkCohort, BenchmarkObservation, BenchmarkQuery, BenchmarkResponse
from .canonical import content_hash

MAX_BYTES = 4_000_000
MAX_ROWS = 1_000
PAGE_SIZE = 100
MAX_PAGES = MAX_ROWS // PAGE_SIZE
DIMENSIONS = ('year_type', 'year', 'quarter', 'state', 'gender', 'sector')
REQUIRED = {'year_type', 'year', 'state', 'gender', 'sector'}


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate MoSPI JSON property')
        result[key] = value
    return result


def _constant(value):
    raise ValueError(f'nonfinite MoSPI number: {value}')


def payload(raw: BenchmarkResponse) -> dict:
    if len(raw.body) > MAX_BYTES:
        raise ValueError('MoSPI response exceeds byte limit')
    value = json.loads(
        raw.body.decode('utf-8'),
        parse_float=Decimal,
        parse_constant=_constant,
        object_pairs_hook=_object,
    )
    if not isinstance(value, dict) or value.get('statusCode') is not True or 'error' in value:
        raise ValueError('MoSPI returned an API failure')
    return value


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('MoSPI label must be nonblank text')
    return value


def _code(value):
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch(r'0|[1-9][0-9]*', value):
        raise ValueError('invalid MoSPI classification code')
    return value


def parse_metadata(indicators: BenchmarkResponse, filters: BenchmarkResponse) -> dict:
    rows = payload(indicators).get('data')
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError('invalid PLFS indicator list')
    selected = [r for r in rows if _code(r.get('indicator_code')) == '6']
    if len(selected) != 1:
        raise ValueError('PLFS salaried earnings indicator is missing or duplicated')
    description = _text(selected[0].get('description'))
    if not all(
        term in description.lower() for term in ('preceding calendar month', 'regular', 'salary')
    ):
        raise ValueError('PLFS earnings indicator definition changed')
    data = payload(filters).get('data')
    if not isinstance(data, dict) or not set(DIMENSIONS) <= data.keys():
        raise ValueError('PLFS required discovery dimensions are missing')
    dimensions, label_codes = {}, {}
    for name in DIMENSIONS:
        entries = data[name]
        if not isinstance(entries, list) or not entries:
            raise ValueError('empty PLFS discovery dimension')
        values, labels = {}, {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError('invalid PLFS discovery entry')
            code = _text(entry.get('year')) if name == 'year' else _code(entry.get(name + '_code'))
            label = code if name == 'year' else _text(entry.get('description'))
            if code in values or label in labels:
                raise ValueError('ambiguous PLFS discovery codes or labels')
            values[code] = label
            labels[label] = code
        dimensions[name] = values
        label_codes[name] = labels
    if (
        dimensions['year_type'].get('1') != 'Agriculture Year'
        or dimensions['year_type'].get('2') != 'Calendar Year'
    ):
        raise ValueError('PLFS year classification changed')
    if dimensions['quarter'] != {'2': 'Jul-Sep', '3': 'Oct-Dec', '4': 'Jan-Mar', '5': 'Apr-Jun'}:
        raise ValueError('PLFS quarter classification changed')
    return {'indicator': description, 'dimensions': dimensions, 'label_codes': label_codes}


def validate_query(query: BenchmarkQuery, *, as_of: date) -> BenchmarkQuery:
    if query.provider != 'mospi' or query.dataset != 'plfs':
        raise ValueError('MoSPI supports only plfs')
    filters = query.filters
    if not filters.keys() >= REQUIRED or not filters.keys() <= set(DIMENSIONS):
        raise ValueError(
            'select explicit year_type, year, state, gender and sector; optional quarter'
        )
    if (
        len(filters['year_type']) != 1
        or filters['year_type'][0] not in ('1', '2')
        or len(filters['year']) != 1
    ):
        raise ValueError('select one PLFS year and year type')
    year = filters['year'][0]
    agriculture = filters['year_type'] == ('1',)
    if not re.fullmatch(r'[0-9]{4}-[0-9]{2}' if agriculture else r'[0-9]{4}', year):
        raise ValueError('use YYYY-YY for Agriculture Year or YYYY for Calendar Year')
    start = int(year[:4])
    if not 2017 <= start <= as_of.year or (agriculture and int(year[5:]) != (start + 1) % 100):
        raise ValueError('invalid PLFS reference year')
    for name in filters.keys() - {'year'}:
        for code in filters[name]:
            _code(code)
    # At most four quarterly observations per explicitly selected cohort.
    if math.prod(len(filters[k]) for k in ('state', 'gender', 'sector')) * 4 > MAX_ROWS:
        raise ValueError('PLFS query exceeds 1000 cells; split into explicit queries')
    return query


def validate_selection(query: BenchmarkQuery, metadata: dict) -> None:
    for name, codes in query.filters.items():
        if not set(codes) <= metadata['dimensions'][name].keys():
            raise ValueError(f'unknown PLFS {name}; use benchmark discover')


def parse_page(raw: BenchmarkResponse, *, page: int) -> tuple[list, tuple[int, int]]:
    value = payload(raw)
    rows, meta = value.get('data'), value.get('meta_data')
    if not isinstance(rows, list) or not isinstance(meta, dict):
        raise ValueError('PLFS data or pagination metadata missing (possibly no data)')
    keys = ('page', 'totalRecords', 'totalPages', 'recordPerPage')
    if any(type(meta.get(k)) is not int for k in keys):
        raise ValueError('invalid PLFS pagination integers')
    total, pages = meta['totalRecords'], meta['totalPages']
    if (
        not 1 <= total <= MAX_ROWS
        or not 1 <= pages <= MAX_PAGES
        or pages != (total + PAGE_SIZE - 1) // PAGE_SIZE
        or not 1 <= page <= pages
        or meta['page'] != page
        or meta['recordPerPage'] != PAGE_SIZE
        or len(rows) != min(PAGE_SIZE, total - (page - 1) * PAGE_SIZE)
    ):
        raise ValueError('inconsistent, incomplete or excessive PLFS pagination')
    return rows, (total, pages)


def reference_period(year: str, year_type: str, quarter: str | None) -> tuple[date, date]:
    first = int(year[:4])
    if quarter is None:
        return (
            (date(first, 7, 1), date(first + 1, 6, 30))
            if year_type == '1'
            else (date(first, 1, 1), date(first, 12, 31))
        )
    month, end_day = {'2': (7, 30), '3': (10, 31), '4': (1, 31), '5': (4, 30)}[quarter]
    actual_year = first + int(year_type == '1' and month < 7)
    return date(actual_year, month, 1), date(actual_year, month + 2, end_day)


def parse_observation(
    row: dict, raw: BenchmarkResponse, query: BenchmarkQuery, metadata: dict, *, as_of: date
) -> BenchmarkObservation:
    fields = {
        'year_type',
        'year',
        'frequency',
        'indicator',
        'state',
        'gender',
        'sector',
        'quarter',
        'month',
        'value',
        'unit',
    }
    if (
        not isinstance(row, dict)
        or not row.keys() >= fields
        or not row.keys() <= fields | {'flags', 'notes'}
    ):
        raise ValueError('invalid PLFS row or unrecognized cohort dimensions')
    if (
        row.get('indicator') != metadata['indicator']
        or row.get('frequency') != 'Annually'
        or row.get('unit') != '\u20b9'
        or row.get('month') is not None
    ):
        raise ValueError('PLFS indicator, frequency, unit or month differs from selected policy')
    selected = {}
    for name in DIMENSIONS:
        label = row.get(name)
        if name == 'quarter' and label is None and name not in query.filters:
            continue
        reverse = metadata['label_codes'][name]
        if not isinstance(label, str) or label not in reverse:
            raise ValueError(f'unknown PLFS {name} label')
        code = reverse[label]
        if name in query.filters and code not in query.filters[name]:
            raise ValueError(f'PLFS returned an unrequested {name}')
        selected[name] = code
    start, end = reference_period(selected['year'], selected['year_type'], selected.get('quarter'))
    if end > as_of:
        raise ValueError('PLFS reference period has not ended')
    if start < date(2025, 1, 1) <= end:
        raise ValueError('PLFS period straddles the January 2025 survey redesign')
    flags, notes = row.get('flags', []), row.get('notes', [])
    if not isinstance(flags, list) or not isinstance(notes, list):
        raise ValueError('invalid PLFS quality annotations')
    flags, notes = tuple(_text(v) for v in flags), tuple(_text(v) for v in notes)
    value = row.get('value')
    amount, status = None, 'available'
    if value == '*':
        status = 'suppressed'
    elif value is None or value in ('-', '..', 'NA'):
        status = 'unavailable'
    else:
        if (
            not isinstance(value, (str, int, Decimal))
            or isinstance(value, bool)
            or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?\+?', str(value))
        ):
            raise ValueError('invalid PLFS earnings value')
        amount = Decimal(str(value).rstrip('+'))
        if str(value).endswith('+'):
            status = 'censored'
    cohort = BenchmarkCohort(
        geography_system='PLFS-state',
        geography_code=selected['state'],
        occupation_system='PLFS-aggregate',
        occupation_code='all',
        population='regular-wage-and-salaried-employees',
        industry_system='PLFS-aggregate',
        industry_code='all',
        working_time='not-separated',
        reference_start=start,
        reference_end=end,
        methodology='PLFS-redesigned-2025-v1' if start.year >= 2025 else 'PLFS-pre-2025-v1',
        wage_definition='plfs_preceding_calendar_month_regular_wage_salary_earnings',
        basis='unknown',
        statistic='mean',
        dimensions=selected
        | {
            'indicator': '6',
            'frequency': '1',
            'year_type_label': row['year_type'],
            'gender_label': row['gender'],
            'sector_label': row['sector'],
            'state_label': row['state'],
        },
    )
    return BenchmarkObservation(
        observation_id=content_hash(selected),
        response_id=raw.response_id,
        cohort=cohort,
        amount=amount,
        currency='INR',
        period='monthly',
        status=status,
        flags=flags,
        notes=(
            *notes,
            'Monthly earnings averaged over the source reference period; annualization is an equivalent, not observed annual pay.',
        ),
    )


def validate_coverage(
    query: BenchmarkQuery, observations: tuple[BenchmarkObservation, ...]
) -> None:
    """Require every explicitly selected cohort, without expanding a Cartesian grid."""
    keys = tuple(sorted(query.filters))
    present = {tuple(row.cohort.dimensions[k] for k in keys) for row in observations}
    if len(present) != math.prod(len(v) for v in query.filters.values()):
        raise ValueError('PLFS omitted an explicitly requested cohort or quarter')
