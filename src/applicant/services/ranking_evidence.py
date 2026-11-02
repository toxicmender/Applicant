"""Capture caller-associated company ratings; no network or fuzzy company matching."""

from datetime import datetime
from decimal import Decimal

from ..domain.canonical import canonical_json, content_hash, utc
from ..domain.ranking import Evidence
from ..domain.ranking_facts import CategoryRating, FactSource, RankingFacts
from ..reviews.models import CompanyRating

_CATEGORIES = {
    'glassdoor': {'culture_and_values', 'career_opportunities'},
    'ambitionbox': {'company_culture', 'career_growth', 'job_security'},
}


def capture_rating(
    rating: CompanyRating, *, verified_company_id: str, observed_at: datetime
) -> tuple[Evidence, Evidence]:
    """The caller establishes job/company association and supplies its provider ID.

    Keep the complete provider payload alongside typed facts, including unsupported
    categories. Stored Decimal strings reflect the legacy float's stated value,
    never a claim of precision that the provider adapter did not retain.
    """
    if rating.company_id != verified_company_id or not verified_company_id.strip():
        raise ValueError('rating does not match the verified provider company ID')
    if rating.source not in _CATEGORIES:
        raise ValueError('unsupported company-rating provider')
    observed_at = utc(observed_at)
    raw = rating.to_dict()
    digest = content_hash(raw)  # reject NaN/non-JSON before producing any evidence
    if rating.review_count is not None and (
        type(rating.review_count) is not int or rating.review_count < 0
    ):
        raise ValueError('review count must be a nonnegative integer or unknown')
    categories = []
    for category, value in sorted(rating.rating_breakdown.items()):
        if category not in _CATEGORIES[rating.source] or rating.review_count == 0:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            raise ValueError('category rating must be numeric')
        source = FactSource(
            evidence_id=f'{digest}:{category}',
            source=rating.source,
            observed_at=observed_at,
            extractor_version='company-rating-1',
            quality='structured',
            excerpt=f'{category}: {value} on a 1–5 scale',
        )
        categories.append(
            CategoryRating.model_validate(
                {
                    'source': source,
                    'company_id': verified_company_id,
                    'company_url': rating.url,
                    'category': category,
                    'value': Decimal(str(value)),
                    'review_count': rating.review_count,
                }
            )
        )
    facts = RankingFacts(ratings=tuple(categories))
    return (
        Evidence(
            source=rating.source,
            observed_at=observed_at,
            extractor_version='company-rating-1',
            payload=facts.model_dump(mode='json'),
        ),
        Evidence.model_validate_json(
            canonical_json(
                Evidence(
                    source=rating.source,
                    observed_at=observed_at,
                    extractor_version='company-rating-raw-1',
                    payload=raw,
                )
            )
        ),
    )
