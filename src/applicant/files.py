"""Reading and writing the JSON files applicant keeps between runs.

Two failure modes this exists to prevent (OWASP Top 10:2025 A10, CWE-390 and
CWE-636 - detecting an error and then failing open):

* a write interrupted part way - a crash, a full disk, Ctrl-C - leaving a torn
  file, so the next run reads nothing and overwrites everything. Writes go to a
  temporary file that replaces the real one only once it is complete.
* an unreadable file being read as empty and then written over, silently
  destroying whatever it held. On a path that is about to write, the unreadable
  file is moved aside and kept; on a path that only reads, it is left alone.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def read_document(path: str | Path, *, quarantine: bool = False) -> dict[str, Any]:
    """The JSON object stored at `path`, or {} when there is none.

    A file that exists but is not a readable JSON object is reported, and with
    `quarantine` moved to `<name>.corrupt-<UTC time>` so the caller's next write
    cannot destroy it. OSError other than a missing file propagates: a file we
    are not allowed to read is not one we should write over either.
    """
    try:
        with open(path, encoding='utf-8') as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return {}
    except (ValueError, UnicodeDecodeError) as error:
        reason = f'{type(error).__name__}: {error}'
    else:
        if isinstance(payload, dict):
            return payload
        reason = f'expected a JSON object, found {type(payload).__name__}'

    if not quarantine:
        logger.warning(f'{path} is unreadable ({reason}); treating it as empty')
        return {}

    kept = f'{path}.corrupt-{time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())}'
    os.replace(path, kept)
    logger.warning(f'{path} is unreadable ({reason}); moved to {kept} and starting afresh')
    return {}


def write_document(path: str | Path, document: Any, *, trailing_newline: bool = False) -> None:
    """Replace `path` with `document`, all at once or not at all."""
    target = Path(path)
    # a data directory named for the first time does not exist yet
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f'.{target.name}.', suffix='.tmp', dir=target.parent
    )
    try:
        # keep an existing file's permissions; a new one stays owner-only (mkstemp)
        if target.exists():
            os.chmod(temporary, stat.S_IMODE(target.stat().st_mode))
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(document, handle, indent=2, ensure_ascii=False)
            if trailing_newline:
                handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        # includes KeyboardInterrupt: the half written temp file must not linger
        Path(temporary).unlink(missing_ok=True)
        raise
    logger.debug(f'wrote {target}')
