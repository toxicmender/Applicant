"""Resolve only recorded, time-valid company identifiers."""

from urllib.parse import urlsplit


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
        if item.verified_at <= knowledge_cutoff
        and (item.effective_from is None or item.effective_from <= effective_date)
        and (item.effective_to is None or effective_date < item.effective_to)
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
