"""Bounded JSON-stat 2.0 decoding and the SES 2022 earnings mapping."""

import json
import math
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import product

from .benchmarks import BenchmarkCohort, BenchmarkObservation, BenchmarkQuery, BenchmarkResponse
from .canonical import content_hash

DATASET = 'earn_ses22_28'
DIMENSIONS = ('freq', 'sex', 'indic_se', 'isco08', 'age', 'sizeclas', 'unit', 'geo', 'time')
DEFAULTS = {
    'freq': ('A',),
    'sex': ('T',),
    'indic_se': ('ERN',),
    'age': ('TOTAL',),
    'sizeclas': ('GE10',),
    'unit': ('EUR',),
    'time': ('2022',),
}
MAX_CELLS = 1_000_000
MAX_FETCH_CELLS = 10_000
MAX_BYTES = 8_000_000


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON property')
        result[key] = value
    return result


def _constant(value):
    raise ValueError(f'nonfinite JSON number: {value}')


def _vector(value, cells, *, flags=False):
    if isinstance(value, str) and flags:
        return dict.fromkeys(range(cells), value)
    if isinstance(value, list):
        if len(value) != cells:
            raise ValueError('JSON-stat vector length does not match cube')
        items = enumerate(value)
    elif isinstance(value, dict):
        if any(not re.fullmatch(r'0|[1-9][0-9]*', k) or int(k) >= cells for k in value):
            raise ValueError('JSON-stat sparse index is outside cube')
        items = ((int(k), v) for k, v in value.items())
    else:
        raise ValueError('invalid JSON-stat vector')
    result = {}
    for index, item in items:
        if item is None:
            continue
        if flags:
            if not isinstance(item, str):
                raise ValueError('JSON-stat status must be text')
        elif (
            isinstance(item, bool)
            or not isinstance(item, (int, Decimal))
            or not Decimal(item).is_finite()
            or item < 0
        ):
            raise ValueError('expected nonnegative finite earnings value')
        result[index] = item
    return result


@dataclass(frozen=True)
class EurostatCube:
    payload: dict
    ids: tuple[str, ...]
    codes: dict[str, tuple[str, ...]]
    values: dict
    statuses: dict


def decode_cube(raw: bytes) -> EurostatCube:
    if len(raw) > MAX_BYTES:
        raise ValueError('Eurostat response exceeds byte limit')
    payload = json.loads(
        raw.decode('utf-8'),
        parse_float=Decimal,
        parse_constant=_constant,
        object_pairs_hook=_object,
    )
    if not isinstance(payload, dict) or 'error' in payload or 'warning' in payload:
        raise ValueError('Eurostat returned an error or asynchronous/incomplete response')
    if payload.get('class') != 'dataset' or payload.get('version') != '2.0':
        raise ValueError('expected JSON-stat 2.0 dataset')
    ids, sizes, dimensions = payload.get('id'), payload.get('size'), payload.get('dimension')
    if (
        not isinstance(ids, list)
        or not ids
        or any(not isinstance(k, str) or not k for k in ids)
        or len(set(ids)) != len(ids)
        or not isinstance(sizes, list)
        or len(ids) != len(sizes)
        or any(type(n) is not int or n <= 0 for n in sizes)
        or not isinstance(dimensions, dict)
        or set(dimensions) != set(ids)
    ):
        raise ValueError('invalid JSON-stat dimensions')
    cells = math.prod(sizes)
    if cells > MAX_CELLS:
        raise ValueError('Eurostat cube exceeds cell limit')
    codes = {}
    for name, size in zip(ids, sizes, strict=True):
        dimension = dimensions[name]
        category = dimension.get('category') if isinstance(dimension, dict) else None
        if not isinstance(category, dict):
            raise ValueError('missing JSON-stat category')
        index = category.get('index')
        if isinstance(index, dict):
            if any(type(n) is not int for n in index.values()) or set(index.values()) != set(
                range(size)
            ):
                raise ValueError('category positions must cover the dimension exactly')
            positions = index
            index = sorted(index, key=lambda k: positions[k])
        if (
            not isinstance(index, list)
            or len(index) != size
            or any(not isinstance(k, str) or not k.strip() for k in index)
            or len(set(index)) != size
        ):
            raise ValueError('invalid category index')
        labels = category.get('label', {})
        if not isinstance(labels, dict) or any(
            k not in index or not isinstance(v, str) for k, v in labels.items()
        ):
            raise ValueError('invalid category labels')
        codes[name] = tuple(index)
    extension = payload.get('extension', {})
    if (
        not isinstance(extension, dict)
        or not isinstance(extension.get('id', DATASET), str)
        or extension.get('id', DATASET).lower() != DATASET
    ):
        raise ValueError('unexpected Eurostat dataset identity')
    return EurostatCube(
        payload,
        tuple(ids),
        codes,
        _vector(payload.get('value'), cells),
        _vector(payload.get('status', {}), cells, flags=True),
    )


def resolve_query(query: BenchmarkQuery, metadata: EurostatCube) -> BenchmarkQuery:
    if query.provider != 'eurostat' or query.dataset != DATASET:
        raise ValueError('Eurostat supports only earn_ses22_28')
    if set(metadata.ids) != set(DIMENSIONS):
        raise ValueError('SES dimension structure changed')
    if not {'geo', 'isco08'} <= query.filters.keys() or not query.filters.keys() <= set(DIMENSIONS):
        raise ValueError(
            'select explicit geo and isco08 codes; only dataset dimensions are accepted'
        )
    filters = DEFAULTS | query.filters
    for key in ('freq', 'indic_se', 'sizeclas', 'unit', 'time'):
        if filters[key] != DEFAULTS[key]:
            raise ValueError(f'initial Eurostat policy requires {key}={DEFAULTS[key][0]}')
    if any(not set(values) <= set(metadata.codes[key]) for key, values in filters.items()):
        raise ValueError('unknown Eurostat category; use benchmark discover')
    if math.prod(len(v) for v in filters.values()) > MAX_FETCH_CELLS:
        raise ValueError('selection exceeds Eurostat fetch cell limit')
    return BenchmarkQuery(provider='eurostat', dataset=DATASET, filters=filters)


def observations(cube: EurostatCube, query: BenchmarkQuery, response: BenchmarkResponse):
    if set(cube.ids) != set(DIMENSIONS) or any(
        set(cube.codes[k]) != set(v) for k, v in query.filters.items()
    ):
        raise ValueError('response does not cover the exact requested categories')
    if math.prod(len(v) for v in cube.codes.values()) > MAX_FETCH_CELLS:
        raise ValueError('selection exceeds Eurostat fetch cell limit')
    results = []
    for index, coordinates in enumerate(product(*(cube.codes[k] for k in cube.ids))):
        row = dict(zip(cube.ids, coordinates, strict=True))
        flag = cube.statuses.get(index, '')
        flags = (flag,) if flag else ()
        observation_flag, separator, confidentiality = flag.partition('|')
        confidential = 'c' in observation_flag.lower() or (
            bool(separator) and confidentiality not in ('', 'F')
        )
        amount = cube.values.get(index)
        status = 'suppressed' if confidential else 'unavailable' if amount is None else 'available'
        cohort = BenchmarkCohort(
            geography_system='Eurostat-GEO',
            geography_code=row['geo'],
            occupation_system='ISCO-08',
            occupation_code=row['isco08'],
            population='SES-employees-enterprises-GE10',
            industry_system='NACE-Rev2',
            industry_code='B-S_X_O',
            working_time='SES-covered-employees',
            reference_start=date(2022, 1, 1),
            reference_end=date(2022, 12, 31),
            methodology='Eurostat-SES-2022-v1',
            wage_definition='gross_annual_earnings_including_irregular_pay',
            basis='gross',
            statistic='mean',
            dimensions={k: row[k] for k in ('freq', 'sex', 'age', 'sizeclas', 'indic_se')},
        )
        results.append(
            BenchmarkObservation(
                observation_id=content_hash(row),
                response_id=response.response_id,
                cohort=cohort,
                amount=amount if status == 'available' else None,
                currency='EUR',
                period='annual',
                status=status,
                flags=flags,
                notes=('Provider-converted EUR; annual earnings include irregular payments.',),
            )
        )
    return tuple(results)
