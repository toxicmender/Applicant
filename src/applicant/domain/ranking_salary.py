"""Conservative Decimal salary extraction; search's legacy parser stays separate."""

from __future__ import annotations

import re
from decimal import Decimal, DecimalException, Inexact, localcontext
from typing import Literal

from iso4217 import Currency

from .contracts import Contract
from .ranking_facts import CompensationFacts, FactSource
from .ranking_math import arithmetic_context
from .salary import AMOUNT, MULTIPLIERS, PERIODS, RANGE


class SalaryParse(Contract):
    extractor_policy: Literal['ranking-salary-1'] = 'ranking-salary-1'
    facts: tuple[CompensationFacts, ...] = ()
    matched_spans: tuple[tuple[int, int], ...] = ()
    warnings: tuple[str, ...] = ()


def decimal_amount(raw: str, *, scaled: bool = False) -> Decimal:
    """Read the legacy lexer's numeric token without passing through float."""
    text = raw.replace('\u00a0', '').replace('\u202f', '').replace(' ', '')
    text = text.replace('’', '').replace("'", '')
    if ',' in text and '.' in text:
        decimal = '.' if text.rfind('.') > text.rfind(',') else ','
        text = text.replace(',' if decimal == '.' else '.', '').replace(decimal, '.')
    elif ',' in text:
        whole, _, tail = text.rpartition(',')
        text = (
            whole + '.' + tail if text.count(',') == 1 and len(tail) <= 2 else text.replace(',', '')
        )
    elif '.' in text and not scaled and all(len(part) == 3 for part in text.split('.')[1:]):
        text = text.replace('.', '')
    return Decimal(text)


def parse_compensation(
    text: str | None,
    *,
    source: FactSource,
    structured: tuple[CompensationFacts, ...] = (),
) -> SalaryParse:
    """Prefer supplied structured tiers; unsupported/ambiguous prose needs review.

    Spans use offsets into original text. Only lexical patterns are shared with
    search: absent units, basis, currency and base component are never defaulted.
    """
    if structured:
        return SalaryParse(facts=structured)
    if not text or not text.strip():
        return SalaryParse(warnings=('salary_missing',))
    if len(text) > 20_000:
        return SalaryParse(warnings=('salary_text_too_long',))
    # One codepoint per replacement keeps spans aligned with the original text.
    cleaned = text.replace('–', '-').replace('—', '-').replace('−', '-')
    # Do not absorb bonuses/equity or retirement-plan numbers into base pay.
    pay = re.split(
        r'\s+(?:plus\b|\+)|,\s*(?:bonus|equity|plus)\b', cleaned, maxsplit=1, flags=re.I
    )[0]
    matches = list(AMOUNT.finditer(pay))
    spans = tuple((match.start(), match.end()) for match in matches)
    warnings = []
    if not matches:
        return SalaryParse(warnings=('salary_bounds_unknown',))
    if re.search(r'\d\s*[eE][+-]?\d', pay):
        return SalaryParse(matched_spans=spans, warnings=('salary_numeric_format_unsupported',))
    if '%' in pay:
        return SalaryParse(matched_spans=spans, warnings=('salary_percentage_unsupported',))
    if len(matches) > 2 or (
        len(matches) == 2 and not RANGE.fullmatch(pay[matches[0].end() : matches[1].start()])
    ):
        return SalaryParse(matched_spans=spans, warnings=('salary_tiers_or_amounts_ambiguous',))
    if re.search(r'[-(]\s*(?:[A-Z]{3}|[€£₹$¥])?\s*$', pay[: matches[0].start()]):
        return SalaryParse(matched_spans=spans, warnings=('salary_negative_or_accounting_amount',))
    if len(matches) == 2 and re.search(r'[-(].*[-(]', pay[matches[0].end() : matches[1].start()]):
        return SalaryParse(matched_spans=spans, warnings=('salary_negative_or_accounting_amount',))
    codes = {token for token in re.findall(r'\b[A-Z]{3}\b', pay) if token in Currency.__members__}
    for symbol, code in (('€', 'EUR'), ('£', 'GBP'), ('₹', 'INR')):
        if symbol in pay:
            codes.add(code)
    for marker, code in (
        ('US$', 'USD'),
        ('CA$', 'CAD'),
        ('AU$', 'AUD'),
        ('NZ$', 'NZD'),
        ('HK$', 'HKD'),
    ):
        if marker in pay:
            codes.add(code)
    code = next(iter(codes)) if len(codes) == 1 else None
    if code is None:
        warnings.append('salary_currency_conflicting' if codes else 'salary_currency_unknown')
    periods = {
        name
        for (pattern, _), name in zip(
            PERIODS, ('hourly', 'daily', 'weekly', 'monthly', 'annual'), strict=True
        )
        if pattern.search(pay)
    }
    period = next(iter(periods)) if len(periods) == 1 else None
    if period is None:
        warnings.append('salary_period_conflicting' if periods else 'salary_period_unknown')
    bases = {value for value in ('gross', 'net') if re.search(r'\b' + value + r'\b', pay, re.I)}
    basis = next(iter(bases)) if len(bases) == 1 else None
    if basis is None:
        warnings.append('salary_basis_conflicting' if bases else 'salary_basis_unknown')
    component = (
        'total'
        if re.search(r'\b(?:total|OTE)\b', pay, re.I)
        else ('base' if re.search(r'\bbase\b', pay, re.I) else 'unknown')
    )
    if component != 'base':
        warnings.append(
            'total_pay_not_base' if component == 'total' else 'salary_base_component_unknown'
        )
    with localcontext(arithmetic_context()) as context:
        try:
            scales = [
                next(
                    (
                        Decimal(factor)
                        for pattern, factor in MULTIPLIERS
                        if pattern.match(match[2] or '')
                    ),
                    Decimal(1),
                )
                for match in matches
            ]
            inherited = max(scales)
            amounts = [
                decimal_amount(match[1], scaled=inherited > 1) * (scale if scale > 1 else inherited)
                for match, scale in zip(matches, scales, strict=True)
            ]
            if context.flags[Inexact]:
                return SalaryParse(matched_spans=spans, warnings=('salary_numeric_range',))
        except DecimalException:
            return SalaryParse(matched_spans=spans, warnings=('salary_numeric_range',))
    if len(amounts) == 2 and amounts[0] > amounts[1]:
        return SalaryParse(matched_spans=spans, warnings=('salary_range_reversed',))
    lower = bool(re.search(r'\b(?:from|starting at|at least)\b|\d\s*[kKmM]?\+', pay, re.I))
    upper = bool(re.search(r'\b(?:up to|at most)\b', pay, re.I))
    if (lower and upper) or (len(amounts) == 2 and (lower or upper)):
        return SalaryParse(matched_spans=spans, warnings=('salary_bounds_ambiguous',))
    low = None if upper else amounts[0]
    high = None if lower else amounts[-1]
    facts = CompensationFacts.model_validate(
        {
            'source': source,
            'original_text': text,
            'minimum': low,
            'maximum': high,
            'shape': 'range'
            if len(amounts) == 2
            else 'lower_only'
            if lower
            else 'upper_only'
            if upper
            else 'exact',
            'currency': code,
            'period': period,
            'basis': basis,
            'component': component,
        }
    )
    return SalaryParse(facts=(facts,), matched_spans=spans, warnings=tuple(warnings))
