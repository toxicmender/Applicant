"""Company financials from Crunchbase and Tracxn, tracked over time.

    from applicant.financials import CrunchbaseClient, TracxnClient, FinancialsTracker

    financials = CrunchbaseClient().fetch('zomato')
    print(financials.total_funding, financials.last_funding_type, financials.valuation)

    changes = FinancialsTracker().record(financials)

Both clients expose the same fetch(company, max_rounds) -> CompanyFinancials, so
callers never have to branch on the source. Each uses the source's API when a key
is set (CRUNCHBASE_API_KEY / TRACXN_API_KEY) and a browser otherwise.
"""

from .crunchbase import CrunchbaseClient
from .errors import AuthError, ChallengeError, CompanyNotFound, FinancialsError, ParseError
from .models import CompanyFinancials, FundingRound, Money
from .parsing import parse_money
from .tracker import FinancialsTracker
from .tracxn import TracxnClient

__all__ = [
    'AuthError',
    'ChallengeError',
    'CompanyFinancials',
    'CompanyNotFound',
    'CrunchbaseClient',
    'FinancialsError',
    'FinancialsTracker',
    'FundingRound',
    'Money',
    'ParseError',
    'TracxnClient',
    'parse_money',
]
