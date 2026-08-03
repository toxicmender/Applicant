from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Review:
    rating: float | None = None
    title: str | None = None
    pros: str | None = None
    cons: str | None = None
    date: str | None = None
    job_title: str | None = None
    location: str | None = None
    source: str = ''

    def to_dict(self):
        return {
            'rating': self.rating,
            'pros': self.pros,
            'cons': self.cons,
            'title': self.title,
            'date': self.date,
            'job_title': self.job_title,
            'location': self.location,
        }


@dataclass
class CompanyRating:
    source: str
    company: str
    url: str
    company_id: str | None = None
    overall_rating: float | None = None
    review_count: int | None = None
    # category name -> rating, e.g. {'work_life_balance': 3.5}
    rating_breakdown: dict = field(default_factory=dict)
    # star -> number of reviews with that star, e.g. {5: 37666}
    rating_distribution: dict = field(default_factory=dict)
    reviews: list = field(default_factory=list)

    def to_dict(self):
        return {
            'source': self.source,
            'company': self.company,
            'company_id': self.company_id,
            'url': self.url,
            'overall_rating': self.overall_rating,
            'review_count': self.review_count,
            'rating_breakdown': self.rating_breakdown,
            'rating_distribution': self.rating_distribution,
            'reviews': [review.to_dict() for review in self.reviews],
        }
