"""Asking several sources in turn without letting one failure cost the rest.

Written three times before - the job search loop, `reviews`, `financials` -
each a little differently. A `SourceError` is expected (a bot check, a missing
company) and logged as a warning; anything else is a bug or an environment
fault, logged as an error with its traceback at debug (OWASP ASVS 16.5.2). Either
way the source is reported through `emit` and the next one is asked.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Generic, TypeVar

from ..errors import SourceError
from .events import Emit, SourceFailed, ignore

logger = logging.getLogger(__name__)

R = TypeVar('R')


@dataclass(frozen=True)
class Outcome(Generic[R]):
    """What one source gave: a result, or the error it failed with."""

    source: str
    result: R | None = None
    error: BaseException | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def fan_out(
    sources: Iterable[str],
    call: Callable[[str], R],
    emit: Emit = ignore,
    subject: str | None = None,
) -> list[Outcome[R]]:
    """`call(source)` for each source, in order, isolating each failure.

    `subject` names what was being fetched ("zomato"), for the log line and
    the event. KeyboardInterrupt and SystemExit are not caught: stopping the
    run is the person's decision, not a source failing.
    """
    outcomes: list[Outcome[R]] = []
    about = f' for {subject!r}' if subject else ''
    for source in sources:
        try:
            result = call(source)
        except SourceError as error:
            logger.warning(f'{source}: {error}')
            emit(SourceFailed(source, error, expected=True, subject=subject))
            outcomes.append(Outcome(source, error=error))
            continue
        except Exception as error:
            logger.error(f'{source}: unexpected {type(error).__name__}{about}: {error}')
            logger.debug(f'{source}: traceback', exc_info=True)
            emit(SourceFailed(source, error, expected=False, subject=subject))
            outcomes.append(Outcome(source, error=error))
            continue
        outcomes.append(Outcome(source, result=result))
    return outcomes
