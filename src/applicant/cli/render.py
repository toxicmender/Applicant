"""Turning what the services report into what the terminal shows.

The one place the package prints progress results. Services emit events and
return answers; this decides how each looks on stdout. Commentary - which board
answered, which one refused - is logging, on stderr, and not repeated here.
"""

from __future__ import annotations

from ..interaction import Interaction, Terminal
from ..services.events import (
    Event,
    FactorFetched,
    FinancialsTracked,
    RatingFetched,
    SourceFailed,
)


def terminal() -> Interaction:
    """Where a command asks the person at the keyboard: stderr and stdin."""
    return Terminal()


class Renderer:
    """An `emit` callback that prints the events a command's answer is made of.

    `failures` also prints a failed source on stdout, beside the results it
    would have stood in for - `financials` does, since a run over many
    companies is read as a table. `rounds` lists each funding round.
    """

    def __init__(self, rounds: bool = False, failures: bool = False):
        self.rounds = rounds
        self.failures = failures

    def __call__(self, event: Event) -> None:
        if isinstance(event, RatingFetched):
            self._rating(event)
        elif isinstance(event, FinancialsTracked):
            self._financials(event)
        elif isinstance(event, SourceFailed):
            self._failure(event)
        elif isinstance(event, FactorFetched):
            self._factor(event)

    def _rating(self, event: RatingFetched) -> None:
        rating = event.rating
        print(
            '{}: {} - {} out of 5 from {} ratings ({} reviews fetched)'.format(
                event.source,
                rating.company,
                rating.overall_rating,
                rating.review_count,
                len(rating.reviews),
            )
        )

    def _financials(self, event: FinancialsTracked) -> None:
        from ..financials.tracker import describe_round

        financials = event.financials
        print('{}: {} - {}'.format(event.source, financials.company, financials.summary()))
        for change in event.changes:
            print('  changed: {}'.format(change))
        for note in financials.notes:
            print('  note: {}'.format(note))
        if self.rounds:
            for item in financials.rounds:
                print('  - {}'.format(describe_round(item)))

    def _failure(self, event: SourceFailed) -> None:
        if not self.failures:
            return
        if event.subject:
            print('{}: {}: {}'.format(event.source, event.subject, event.error))
        else:
            print('{}: {}'.format(event.source, event.error))

    def _factor(self, event: FactorFetched) -> None:
        if event.skipped:
            return
        if event.entry is None:
            print('  {}: no value returned'.format(event.country))
        else:
            print('  {}: {} ({})'.format(event.country, event.entry['value'], event.entry['year']))
