"""Authentication fixtures contain only synthetic, allowlisted DOM controls."""

from html.parser import HTMLParser
from pathlib import Path

import pytest


class FixtureMarkup(HTMLParser):
    tags = frozenset({'a', 'aside', 'button', 'div', 'nav', 'section', 'span', 'ul'})
    attrs = frozenset({
        'aria-label',
        'class',
        'data-gnav-element-name',
        'data-signed-in',
        'data-testid',
        'hidden',
        'href',
        'id',
    })
    links = frozenset({'https://accounts.google.com/SignOutOptions'})

    def handle_starttag(self, tag, attrs):
        assert tag in self.tags, tag
        for key, value in attrs:
            assert key in self.attrs, key
            if key == 'href':
                assert value in self.links, key
            elif key == 'hidden':
                assert value in (None, ''), key
            else:
                assert value and value.isascii(), key
                assert len(value) < 80 and all(c.isalnum() or c in ' -_' for c in value), key
                if key == 'aria-label':
                    assert value in {'Account', 'Open profile menu', 'Profile'}, key

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
