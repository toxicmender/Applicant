"""Authentication fixtures contain only synthetic, allowlisted DOM controls."""

from html.parser import HTMLParser
from pathlib import Path

import pytest


class FixtureMarkup(HTMLParser):
    tags = frozenset({'a', 'aside', 'button', 'div', 'nav', 'section', 'span', 'ul'})
    values = (
        ('aria-label', frozenset({'Account', 'Open profile menu', 'Profile'})),
        ('class', frozenset({'site-nav__account', 'site-nav__user', 'site-nav__auth'})),
        ('data-gnav-element-name', frozenset({'Inbox', 'SignIn'})),
        ('data-signed-in', frozenset({'true', 'false'})),
        (
            'data-testid',
            frozenset(
                {
                    'identity-module',
                    'identity-toggle-trigger',
                    'left-rail-my-bowls',
                    'userProfile',
                    'inboxBell',
                }
            ),
        ),
        ('hidden', frozenset({None, ''})),
        ('href', frozenset({'https://accounts.google.com/SignOutOptions', '/login'})),
        ('id', frozenset({'AccountMenu', 'login_Layer'})),
    )

    def handle_starttag(self, tag, attrs):
        assert tag in self.tags, tag
        allowed = dict(self.values)
        for key, value in attrs:
            assert key in allowed and value in allowed[key], key

    def handle_data(self, data):
        assert not data.strip(), 'fixture elements must be empty'

    def handle_comment(self, data):
        raise AssertionError('fixture comments are not allowed')


def check_markup(markup):
    FixtureMarkup().feed(markup)


@pytest.mark.parametrize(
    'markup',
    [
        '<script></script>',
        '<input type="hidden" value="token">',
        '<div id="sessionid12345"></div>',
        '<div nonce="secret"></div>',
        '<div data-json="secret"></div>',
        '<div aria-label="Person"></div>',
        '<a href="/profile?id=123"></a>',
        '<span>person@example.test</span>',
        '<!-- hidden token -->',
    ],
)
def test_rejects_sensitive_markup(markup):
    with pytest.raises(AssertionError):
        check_markup(markup)


def test_all_authentication_fixtures_are_minimal():
    folder = Path(__file__).parent / 'fixtures' / 'provider_auth'
    for fixture in folder.glob('*.html'):
        check_markup(fixture.read_text(encoding='utf-8'))
