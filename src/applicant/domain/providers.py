"""Provider catalog: capabilities describe implemented operations, not aspirations."""

from dataclasses import asdict, dataclass, replace


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    url: str
    domains: tuple[str, ...]
    access: str = 'import_reference'
    board: str | None = None
    aliases: tuple[str, ...] = ()
    login_url: str | None = None
    check_url: str | None = None
    auth_selector: str | None = None
    validation: str = 'reference_only'

    def document(self):
        return asdict(self)


PROVIDERS = {
    p.id: p
    for p in (
        Provider(
            'linkedin',
            'LinkedIn',
            'https://www.linkedin.com',
            ('jobs',),
            'public_session',
            'applicant.boards.linkedin.LinkedInGuest',
            login_url='https://www.linkedin.com/login',
            check_url='https://www.linkedin.com/feed/',
            auth_selector='a[href*="/mynetwork/"]',
            validation='existing_adapter',
        ),
        Provider(
            'indeed',
            'Indeed',
            'https://www.indeed.com',
            ('jobs',),
            'public',
            'applicant.boards.indeed.Indeed',
            validation='existing_adapter',
        ),
        Provider(
            'naukri',
            'Naukri',
            'https://www.naukri.com',
            ('jobs',),
            'public',
            'applicant.boards.naukri.Naukri',
            validation='existing_adapter',
        ),
        Provider(
            'googlejobs',
            'Google Jobs',
            'https://www.google.com',
            ('jobs',),
            'public',
            'applicant.boards.googlejobs.GoogleJobs',
            validation='existing_adapter',
        ),
        Provider(
            'glassdoor',
            'Glassdoor',
            'https://www.glassdoor.com',
            ('culture', 'compensation'),
            'session_import',
            login_url='https://www.glassdoor.com/profile/login_input.htm',
            check_url='https://www.glassdoor.com/member/profile/index.htm',
            auth_selector='[data-test="user-profile-menu"], a[href*="logout"]',
            validation='existing_adapter',
        ),
        Provider(
            'ambitionbox',
            'AmbitionBox',
            'https://www.ambitionbox.com',
            ('culture', 'compensation'),
            'public',
            validation='existing_adapter',
        ),
        Provider(
            'crunchbase',
            'Crunchbase',
            'https://www.crunchbase.com',
            ('company',),
            'session_api_import',
            login_url='https://www.crunchbase.com/login',
            check_url='https://www.crunchbase.com/account',
            auth_selector='a[href*="logout"], button[aria-label="Account"]',
            validation='existing_adapter',
        ),
        Provider(
            'tracxn',
            'Tracxn',
            'https://tracxn.com',
            ('company',),
            'session_api_import',
            login_url='https://tracxn.com/login',
            check_url='https://tracxn.com/',
            auth_selector='a[href*="logout"], [data-testid="user-menu"]',
            validation='existing_adapter',
        ),
        Provider(
            'weworkremotely',
            'We Work Remotely',
            'https://weworkremotely.com',
            ('jobs', 'workplace'),
            'public_feed',
            'applicant.boards.weworkremotely.WeWorkRemotely',
            aliases=('wwr',),
            validation='fixture_pending',
        ),
        Provider(
            'levels',
            'Levels.fyi',
            'https://www.levels.fyi',
            ('compensation',),
            aliases=('levels.fyi',),
        ),
        Provider('nodeflair', 'NodeFlair', 'https://nodeflair.com', ('compensation', 'jobs')),
        Provider('payscale', 'Payscale', 'https://www.payscale.com', ('compensation',)),
        Provider(
            'comprehensive', 'Comprehensive', 'https://www.comprehensive.io', ('compensation',)
        ),
        Provider('blind', 'Blind', 'https://www.teamblind.com', ('culture', 'workplace')),
        Provider(
            'wellfound', 'Wellfound', 'https://wellfound.com', ('jobs', 'company', 'workplace')
        ),
        Provider(
            'builtin',
            'Built In',
            'https://builtin.com',
            ('jobs', 'company', 'culture', 'workplace'),
        ),
        Provider(
            'welcometothejungle',
            'Welcome to the Jungle',
            'https://www.welcometothejungle.com',
            ('jobs', 'company'),
            aliases=('otta',),
        ),
        Provider('arc', 'Arc', 'https://arc.dev', ('jobs', 'workplace')),
        Provider('instahyre', 'Instahyre', 'https://www.instahyre.com', ('jobs',)),
        Provider('flexa', 'Flexa', 'https://flexa.careers', ('company', 'workplace')),
        Provider('dice', 'Dice', 'https://www.dice.com', ('jobs',)),
        Provider('ziprecruiter', 'ZipRecruiter', 'https://www.ziprecruiter.com', ('jobs',)),
        Provider('turing', 'Turing', 'https://www.turing.com', ('jobs',)),
        Provider('upwork', 'Upwork', 'https://www.upwork.com', ('jobs',)),
        Provider('recrew', 'Recrew', 'https://www.recrew.ai', ('company', 'compensation')),
    )
}
DEFAULT_BOARDS = ('linkedin', 'indeed', 'naukri', 'googlejobs')

# These adapters accept only public structured data. Fixture support does not
# assert that a provider currently permits or exposes live acquisition.
for _name in ('wellfound', 'builtin', 'welcometothejungle', 'arc', 'instahyre'):
    PROVIDERS[_name] = replace(
        PROVIDERS[_name],
        board='applicant.boards.public.PublicBoard',
        access='public_structured_or_import',
        validation='fixture_validated_live_unverified',
    )
PROVIDERS['weworkremotely'] = replace(
    PROVIDERS['weworkremotely'], validation='fixture_validated_live_unverified'
)


def provider(name: str) -> Provider:
    for item in PROVIDERS.values():
        if name == item.id or name in item.aliases:
            return item
    raise ValueError(f'unknown provider: {name}')


AVAILABLE_BOARDS = tuple(name for name, item in PROVIDERS.items() if item.board)
