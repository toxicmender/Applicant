"""Corporate chronology is independent of the report's display timezone."""

from datetime import datetime, time, timedelta, timezone

from .research_time import zone


def event_bounds(event):
    exact = getattr(event, 'effective_at', None)
    if exact:
        instant = datetime.fromisoformat(exact.replace('Z', '+00:00')).astimezone(timezone.utc)
        return instant, instant
    if event.effective_date is None:
        return None, None
    day = event.effective_date
    location = getattr(event, 'effective_timezone', None)
    if location:
        tz = zone(location)
        return (
            datetime.combine(day, time(), tz).astimezone(timezone.utc),
            datetime.combine(day + timedelta(days=1), time(), tz).astimezone(timezone.utc),
        )
    start = datetime.combine(day, time(), timezone.utc)
    return start - timedelta(hours=14), start + timedelta(days=1, hours=12)


def event_phase(event, observation, cutoff):
    start, end = event_bounds(event)
    if start is None or end is None or observation is None or end >= cutoff:
        return 'timing-indeterminate'
    if observation < start:
        return 'historical'
    if observation >= end:
        return 'post-event contextual'
    return 'timing-indeterminate'
