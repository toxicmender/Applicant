"""Version 1 canonical JSON: explicit defaults/nulls, ordered lists, decimal strings.

Object keys sort; decimals use fixed notation without insignificant trailing zeros;
aware timestamps become UTC. Strings retain case and Unicode. Hash projections
select their own behavior-affecting fields. Changing these rules requires v2.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from pydantic import BaseModel


def utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError('timestamp must be timezone-aware')
    return value.astimezone(timezone.utc)


def _value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _value(value.model_dump(mode='python'))
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError('decimal must be finite')
        text = format(value, 'f')
        return '0' if value == 0 else text.rstrip('0').rstrip('.') if '.' in text else text
    if isinstance(value, datetime):
        return utc(value).isoformat(timespec='microseconds').replace('+00:00', 'Z')
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_value(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        _value(value), sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False
    )


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()
