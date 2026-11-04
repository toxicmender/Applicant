"""Build existing company clients with bounded raw-response capture."""

import httpx

from ..domain.company_evidence import CompanyResponse
from ..errors import Unparseable
from ..financials import CrunchbaseClient, TracxnClient
from ..reviews import AmbitionBoxClient, GlassdoorClient


class CompanyRecorder:
    def __init__(self):
        self.responses: dict[str, CompanyResponse] = {}
        self.size = 0
        self.failure: str | None = None

    def __call__(self, url, body, representation):
        # No request headers, cookies or query credentials enter the archive.
        safe_url = str(
            httpx.URL(url).copy_with(query=None, fragment=None, username='', password='')
        )
        if len(body) > 8_000_000 or self.size + len(body) > 16_000_000 or len(self.responses) >= 64:
            # Browser event handlers may suppress exceptions. Remember failure too.
            self.failure = 'company raw capture exceeds byte/response limits'
            raise Unparseable(self.failure)
        response = CompanyResponse.model_validate(
            CompanyResponse.capture(safe_url, body).model_dump()
            | {'representation': representation}
        )
        if response.response_id not in self.responses:
            self.size += len(body)
            self.responses[response.response_id] = response

    def completed(self):
        if self.failure or not self.responses:
            raise Unparseable(self.failure or 'company client returned no raw capture')
        return tuple(self.responses.values())


def company_client(association, recorder, *, settings):
    provider = association.provider
    if provider == 'ambitionbox':
        return AmbitionBoxClient(capture=recorder)
    if provider == 'glassdoor':
        return GlassdoorClient(
            profile_dir=settings.path(settings.files.glassdoor_profile), capture=recorder
        )
    if provider == 'crunchbase':
        return CrunchbaseClient(
            api_key=settings.secret('crunchbase_key'),
            profile_dir=settings.path(settings.files.crunchbase_profile),
            capture=recorder,
        )
    web = association.locator.startswith('https://')
    key = '' if web else settings.secret('tracxn_key')
    if not key and not web:
        raise ValueError(
            'Tracxn exact entity IDs require an API key; web captures require an exact profile association'
        )
    return TracxnClient(
        api_key=key,
        profile_dir=settings.path(settings.files.tracxn_profile),
        capture=recorder,
    )
