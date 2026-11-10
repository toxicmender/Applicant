"""Pinned timezone conversion and source-date freshness; no headquarters inference."""

import calendar
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from importlib import resources
from zoneinfo import ZoneInfo

import tzdata


@lru_cache(maxsize=256)
def zone(name):
    if name == 'EST':
        return timezone(timedelta(hours=-5), name='EST')
    if '/' not in name and name != 'UTC':
        raise ValueError('ambiguous timezone abbreviation; use an IANA region or explicit EST')
    if any(part in ('', '.', '..') for part in name.split('/')):
        raise ValueError('invalid timezone')
    try:
        with resources.files('tzdata.zoneinfo').joinpath(*name.split('/')).open('rb') as source:
            return ZoneInfo.from_file(source, key=name)
    except (OSError, ValueError):
        raise ValueError(f'unknown timezone: {name}') from None


def localize(day, clock, name, fold=None):
    naive = datetime.combine(day, time.fromisoformat(clock))
    tz = zone(name)
    valid = []
    for candidate_fold in (0, 1):
        value = naive.replace(tzinfo=tz, fold=candidate_fold)
        if value.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) == naive:
            valid.append(value)
    if not valid:
        raise ValueError('nonexistent local time during DST transition')
    if len({v.utcoffset() for v in valid}) > 1 and fold is None:
        raise ValueError('ambiguous local time; specify fold 0 or 1')
    return next(v for v in valid if fold is None or v.fold == fold)


def convert_schedule(schedule, applicant_timezone, day):
    start = localize(day, schedule.start, schedule.employer_timezone, schedule.fold)
    end_day = day + timedelta(days=int(schedule.end <= schedule.start))
    end = localize(end_day, schedule.end, schedule.employer_timezone, schedule.fold)
    target = zone(applicant_timezone)
    return {
        'employer_timezone': schedule.employer_timezone,
        'applicant_timezone': applicant_timezone,
        'date': day.isoformat(),
        'source_start': start.isoformat(),
        'source_end': end.isoformat(),
        'local_start': start.astimezone(target).isoformat(),
        'local_end': end.astimezone(target).isoformat(),
        'tzdata_version': tzdata.__version__,
    }


def seasonal_schedules(schedule, applicant_timezone, day):
    variants = {}
    for offset in range(366):
        selected = day + timedelta(days=offset)
        converted = convert_schedule(schedule, applicant_timezone, selected)
        signature = (
            converted['source_start'][-6:],
            converted['local_start'][11:],
            converted['local_end'][11:],
        )
        variants.setdefault(signature, converted)
    return list(variants.values())


def freshness(evidence, as_of, thresholds=None):
    thresholds = thresholds or {}
    kind = evidence.payload.kind
    source_date = evidence.observed_at.date() if evidence.observed_at else None
    if source_date is None and kind == 'policy':
        source_date = evidence.payload.effective_date
        if source_date and source_date > as_of:
            return 'not_yet_effective'
    if source_date is None:
        return 'freshness_unknown'
    if kind == 'compensation':
        months = thresholds.get('compensation_months', 18)
        index = as_of.year * 12 + as_of.month - 1 - months
        year, month = divmod(index, 12)
        month += 1
        cutoff = date(year, month, min(as_of.day, calendar.monthrange(year, month)[1]))
    else:
        cutoff = as_of - timedelta(days=thresholds.get(kind, 90 if kind == 'policy' else 365))
    return 'stale' if source_date < cutoff else 'current'
