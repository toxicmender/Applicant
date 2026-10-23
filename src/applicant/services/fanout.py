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

from ..errors import ApplicantError, SourceError
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
    run is the person's decision, not a source failing. Nor is an
    `ApplicantError` that is not a `SourceError` - a `StoreError`, a
    `ConfigError`: that is the whole run's problem, not the source's.
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
        except ApplicantError:
            # a store that cannot be written, a setting that does not hold: the
            # whole run's problem, not this source's - and the next source would
            # only meet it again
            raise
        except Exception as error:
            logger.error(f'{source}: unexpected {type(error).__name__}{about}: {error}')
            logger.debug(f'{source}: traceback', exc_info=True)
            emit(SourceFailed(source, error, expected=False, subject=subject))
            outcomes.append(Outcome(source, error=error))
            continue
        outcomes.append(Outcome(source, result=result))
    return outcomes


def failure(outcomes: Iterable[Outcome]) -> SourceError | None:
    """The error to stop on when no source succeeded, or None.

    One source failing is isolated; every source failing is the run failing,
    and the caller raises this so the exit code says why (blocked, unreachable,
    not found) rather than "nothing matched". Only when every failure was an
    expected `SourceError` - an unexpected one is already exit 1 either way.
    """
    # a source that succeeded has no error, and so fails the test below too
    errors = [outcome.error for outcome in outcomes]
    if errors and all(isinstance(error, SourceError) for error in errors):
        first = errors[0]
        assert isinstance(first, SourceError)
        return first
    return None
