from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator

SYMBOLS = {'USD': '$', 'INR': '₹', 'EUR': '€', 'GBP': '£', 'JPY': '¥'}


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Money(BaseModel):
    """An amount as the source stated it, plus its USD value when the source gave one."""

    model_config = ConfigDict(str_strip_whitespace=True)

    amount: float | None = None
    currency: str | None = None
    amount_usd: float | None = None
    text: str | None = None  # what the source actually said, e.g. '$2.1B'

    def same_as(self, other):
        if other is None:
            return False
        if self.amount_usd is not None and other.amount_usd is not None:
            return round(self.amount_usd) == round(other.amount_usd)
        return (self.currency, round(self.amount or 0)) == (
            other.currency,
            round(other.amount or 0),
        )

    def __str__(self):
        if self.amount is None:
            return self.text or 'unknown'
        return compact(self.amount, self.currency)


def compact(amount, currency=None):
    """2_100_000_000, 'USD' -> '$2.1B'."""
    prefix = SYMBOLS.get(currency or '', (currency + ' ') if currency else '')
    for size, suffix in ((1e12, 'T'), (1e9, 'B'), (1e6, 'M'), (1e3, 'K')):
        if abs(amount) >= size:
            return '{}{}{}'.format(
                prefix, '{:.2f}'.format(amount / size).rstrip('0').rstrip('.'), suffix
            )
    return '{}{:,.0f}'.format(prefix, amount)


class FundingRound(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    date: str | None = None  # ISO date the round was announced
    round: str | None = None  # 'Series A', 'Seed', 'Debt Financing'
    amount: Money | None = None
    post_money_valuation: Money | None = None
    lead_investors: list[str] = Field(default_factory=list)
    investor_count: int | None = None

    @field_validator('date', mode='before')
    @classmethod
    def _iso_date(cls, value):
        return value[:10] if isinstance(value, str) and len(value) >= 10 else value

    def key(self):
        """Identity across fetches, for spotting new rounds."""
        return self.date, (self.round or '').lower()


class CompanyFinancials(BaseModel):
    """What a source knows about a company's money, normalised across sources.

    Every field is optional because every source is partial: Crunchbase gives a
    revenue band where Tracxn gives a figure, free plans hide half the fields,
    and logged-out pages blur the rest. None means 'not known', never 'zero'.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    source: str
    company: str
    url: str | None = None
    company_id: str | None = None  # Crunchbase permalink or Tracxn entity id
    fetched_at: str = Field(default_factory=_now)
    description: str | None = None
    website: str | None = None
    founded: str | None = None
    stage: str | None = None  # 'Series C', 'Public', 'Acquired', ...
    ipo_status: str | None = None
    stock_symbol: str | None = None
    employees: str | None = None  # a band like '1001-5000', or a count
    total_funding: Money | None = None
    funding_rounds_count: int | None = None
    last_funding_type: str | None = None
    last_funding_date: str | None = None
    valuation: Money | None = None
    valuation_date: str | None = None
    revenue: Money | None = None  # a figure, where the source has one
    revenue_year: str | None = None
    revenue_range: str | None = None  # a band, where that is all the source has
    rounds: list[FundingRound] = Field(default_factory=list)
    # which parts could not be had and why, so a gap is never mistaken for a zero
    notes: list[str] = Field(default_factory=list)

    @field_validator('last_funding_date', 'valuation_date', mode='before')
    @classmethod
    def _iso_date(cls, value):
        return value[:10] if isinstance(value, str) and len(value) >= 10 else value

    def fill_from_rounds(self):
        """Derive the headline figures a source left out from its own round list."""
        dated = sorted((r for r in self.rounds if r.date), key=lambda r: r.date or '', reverse=True)
        if dated:
            latest = dated[0]
            if self.last_funding_date is None:
                self.last_funding_date = latest.date
            if self.last_funding_type is None:
                self.last_funding_type = latest.round
            if self.valuation is None:
                for item in dated:
                    if item.post_money_valuation is not None:
                        self.valuation = item.post_money_valuation
                        self.valuation_date = item.date
                        break
        if self.funding_rounds_count is None and self.rounds:
            self.funding_rounds_count = len(self.rounds)
        return self

    def summary(self):
        parts = []
        if self.total_funding is not None:
            text = 'raised {}'.format(self.total_funding)
            if self.funding_rounds_count:
                text += ' over {} round{}'.format(
                    self.funding_rounds_count, '' if self.funding_rounds_count == 1 else 's'
                )
            parts.append(text)
        if self.last_funding_type or self.last_funding_date:
            parts.append(
                'last {}'.format(
                    ' on '.join(
                        value for value in (self.last_funding_type, self.last_funding_date) if value
                    )
                )
            )
        if self.valuation is not None:
            parts.append(
                'valued at {}{}'.format(
                    self.valuation,
                    ' ({})'.format(self.valuation_date) if self.valuation_date else '',
                )
            )
        if self.revenue is not None:
            parts.append(
                'revenue {}{}'.format(
                    self.revenue, ' ({})'.format(self.revenue_year) if self.revenue_year else ''
                )
            )
        elif self.revenue_range:
            parts.append('revenue {}'.format(self.revenue_range))
        if self.stage:
            parts.append(self.stage)
        return ', '.join(parts) or 'no financial data found'

    def to_dict(self):
        return self.model_dump(mode='json')
