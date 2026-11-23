"""Provider catalog: capabilities describe implemented operations, not aspirations."""

from dataclasses import asdict, dataclass, replace
from urllib.parse import urlsplit


@dataclass(frozen=True)
class VerificationTarget:
    url: str
    markers: tuple[str, ...]
    signed_out: tuple[str, ...] = ()
    region: str | None = None


@dataclass(frozen=True)
class SessionPolicy:
    login: str
    targets: tuple[VerificationTarget, ...]
    storage_domains: tuple[str, ...]
    public_access: bool = False


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
    authentication: SessionPolicy | None = None

    @property
    def session(self) -> SessionPolicy | None:
        if self.authentication:
            return self.authentication
        if not self.login_url or not self.check_url or not self.auth_selector:
            return None
        hostname = urlsplit(self.url).hostname
        assert hostname
        return SessionPolicy(
            self.login_url,
            (VerificationTarget(self.check_url, (self.auth_selector,)),),
            (hostname.removeprefix('www.'),),
        )

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
            check_url='https://platform.tracxn.com/a/dashboard',
            auth_selector='#r-dashboard-wrap .txn--home-dashboard',
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
INDEED_HOSTS = (
    'www',
    'in',
    'uk',
    'ca',
    'au',
    'de',
    'fr',
    'sg',
    'ie',
    'nz',
    'za',
    'ae',
    'nl',
    'se',
    'ch',
)


def _add_session(name, policy):
    target = policy.targets[0]
    PROVIDERS[name] = replace(
        PROVIDERS[name],
        authentication=policy,
        login_url=policy.login,
        check_url=target.url,
        auth_selector=target.markers[0],
    )


GLASSDOOR_REGIONS = ('com', 'co.in')

_add_session(
    'glassdoor',
    SessionPolicy(
        'https://www.glassdoor.co.in/Community/index.htm',
        (
            VerificationTarget(
                'https://www.glassdoor.co.in/Community/index.htm',
                (
                    '[data-testid="identity-module"] button[data-testid="identity-toggle-trigger"]',
                    '[data-testid="left-rail-my-bowls"]',
                ),
                ('button:text-is("Sign in"), a:text-is("Sign in")',),
                region='co.in',
            ),
            VerificationTarget(
                'https://www.glassdoor.com/member/profile/index.htm',
                ('[data-test="user-profile-menu"], a[href*="logout"]',),
                region='com',
            ),
        ),
        tuple(f'glassdoor.{region}' for region in GLASSDOOR_REGIONS),
        True,
    ),
)
_add_session(
    'indeed',
    SessionPolicy(
        'https://in.indeed.com/',
        tuple(
            VerificationTarget(
                f'https://{host}.indeed.com/',
                ('button#AccountMenu', 'a[data-gnav-element-name="Inbox"]'),
                ('a[data-gnav-element-name="SignIn"]',),
            )
            for host in ('in', *(host for host in INDEED_HOSTS if host != 'in'))
        ),
        ('indeed.com',),
        True,
    ),
)
_add_session(
    'naukri',
    SessionPolicy(
        'https://www.naukri.com/nlogin/login',
        (
            VerificationTarget(
                'https://www.naukri.com/mnjuser/profile',
                ('button[aria-label="Open profile menu"]',),
                ('#login_Layer',),
            ),
        ),
        ('naukri.com',),
        True,
    ),
)
_GOOGLE_ACCOUNT = 'a[href^="https://accounts.google.com/SignOutOptions"]'
_GOOGLE_SIGN_IN = 'a[href^="https://accounts.google.com/ServiceLogin"]'
_add_session(
    'googlejobs',
    SessionPolicy(
        'https://www.google.com/about/careers/applications/jobs/results',
        tuple(
            VerificationTarget(
                f'https://{host}/about/careers/applications/jobs/results',
                (_GOOGLE_ACCOUNT,),
                (_GOOGLE_SIGN_IN,),
            )
            for host in ('www.google.com', 'www.google.co.in')
        ),
        ('google.com', 'google.co.in'),
        True,
    ),
)
_add_session(
    'ambitionbox',
    SessionPolicy(
        'https://www.ambitionbox.com/',
        (
            VerificationTarget(
                'https://www.ambitionbox.com/',
                ('button[data-testid="userProfile"]', 'button[data-testid="inboxBell"]'),
                ('button:text-is("Login")',),
            ),
        ),
        ('ambitionbox.com',),
        True,
    ),
)
_add_session(
    'levels',
    SessionPolicy(
        'https://www.levels.fyi/community',
        (
            VerificationTarget(
                'https://www.levels.fyi/community',
                (
                    '.site-nav__account[data-signed-in="true"] .site-nav__user button[aria-label="Profile"]',
                ),
                ('.site-nav__account[data-signed-in="false"] .site-nav__auth a[href="/login"]',),
            ),
        ),
        ('levels.fyi',),
    ),
)
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
