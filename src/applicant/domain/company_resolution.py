"""Resolve verified identifiers and suggest dated legal-name matches."""

import unicodedata
from urllib.parse import urlsplit


def _active(known_at, item, effective_date, knowledge_cutoff):
    return (
        known_at <= knowledge_cutoff
        and (item.effective_from is None or item.effective_from <= effective_date)
        and (item.effective_to is None or effective_date < item.effective_to)
    )


def resolve_company(
    identifiers,
    *,
    effective_date,
    knowledge_cutoff,
    provider=None,
    provider_id=None,
    locator=None,
    legal_id=None,
    domain=None,
):
    """Return a unique entity, or None for insufficient or conflicting evidence."""
    active = [
        item
        for item in identifiers
        if _active(item.verified_at, item, effective_date, knowledge_cutoff)
    ]
    candidates = [
        {
            item.entity_id
            for item in active
            if provider is not None
            and provider_id is not None
            and item.provider == provider
            and item.provider_company_id == provider_id
            and (locator is None or item.verification_url == locator)
        },
        {
            item.entity_id
            for item in active
            if legal_id is not None
            and (item.provider, item.provider_company_id) == legal_id
            and item.provider in ('lei', 'cik')
        },
        {
            item.entity_id
            for item in active
            if domain is not None
            and item.provider == 'domain'
            and item.provider_company_id.lower() == domain.lower()
            and urlsplit(item.verification_url).hostname == domain.lower()
        },
    ]
    first = next((group for group in candidates if group), set())
    if len(first) != 1 or any(group and group != first for group in candidates):
        return None
    return next(iter(first))


def suggest_manual_matches(
    names, identifiers, *, legal_name, country, effective_date, knowledge_cutoff
):
    """List candidates for human verification, with no automatic association."""

    def normalize(value):
        text = unicodedata.normalize('NFKC', value).casefold().replace('&', ' and ')
        return tuple(''.join(char if char.isalnum() else ' ' for char in text).split())

    expected = normalize(legal_name)
    by_country = {
        item.entity_id
        for item in identifiers
        if item.provider == 'legal-country'
        and item.provider_company_id == country
        and _active(item.verified_at, item, effective_date, knowledge_cutoff)
    }
    return sorted(
        {
            item.entity_id
            for item in names
            if item.entity_id in by_country
            and item.name_type == 'legal'
            and expected
            and normalize(item.name) == expected
            and _active(item.recorded_at, item, effective_date, knowledge_cutoff)
        }
    )
