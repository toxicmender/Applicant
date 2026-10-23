"""Save a live page, and the API responses it makes, as a test fixture.

    uv run python tools/record_page.py URL OUT.html [--capture REGEX OUT.json]
        [--profile DIR] [--show] [--redact REGEX]

The pages in tests/fixtures/pages are written to the markup the code
expects; the live sites cannot be reached from CI. Run this from a machine that
can reach them to replace one with the real thing, then run
tests/test_live_pages.py against it - a failure there is the site having
changed under the selectors.

What is saved is stripped of what identifies a person: email addresses, and
anything matching --redact (a reviewer's name, say). Read the files before
committing them all the same. Use --show to clear a bot check or sign in by
hand first; the page is saved once you press Enter.

The page is opened the way the sources open it - applicant's own launcher,
an installed Chrome before Edge before the bundled build - since Naukri and
Google refuse the bundled build outright. --profile reuses a source's
profile (.cb_profile, .gd_profile) where a person already cleared Cloudflare.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from contextlib import suppress
from pathlib import Path

EMAIL = re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+')


def scrub(text: str, patterns: list[re.Pattern[str]]) -> str:
    text = EMAIL.sub('person@example.com', text)
    for pattern in patterns:
        text = pattern.sub('REDACTED', text)
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or '').split('\n')[0])
    parser.add_argument('url')
    parser.add_argument('out', type=Path, help='where to write the page html')
    parser.add_argument(
        '--capture',
        nargs=2,
        metavar=('REGEX', 'OUT'),
        help='also save the first JSON response whose url matches REGEX',
    )
    parser.add_argument('--redact', action='append', default=[], metavar='REGEX')
    parser.add_argument('--profile', help="a source's browser profile directory to reuse")
    parser.add_argument('--show', action='store_true', help='a visible browser; wait for Enter')
    args = parser.parse_args(argv)

    from applicant.infra.browser import BrowserSession

    redact = [re.compile(pattern) for pattern in args.redact]
    captured: list = []
    wanted = re.compile(args.capture[0]) if args.capture else None

    def hear(response):
        if wanted is None or captured or not wanted.search(response.url):
            return
        try:
            captured.append(response.json())
        except Exception:  # noqa: BLE001 - not JSON; keep listening
            return

    with BrowserSession(args.profile, headless=not args.show) as session:
        page = session.start()
        page.on('response', hear)
        page.goto(args.url, wait_until='domcontentloaded')
        # as the sources wait: some of these pages never go network-idle
        with suppress(Exception):
            page.wait_for_load_state('networkidle', timeout=15_000)
        if args.show:
            input('Clear any check or sign in, then press Enter to save the page... ')
        html = page.content()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(scrub(html, redact), encoding='utf-8')
    print(f'page written to {args.out}')
    if args.capture:
        if not captured:
            print(f'no response matched {args.capture[0]!r}', file=sys.stderr)
            return 1
        target = Path(args.capture[1])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(scrub(json.dumps(captured[0], indent=2), redact), encoding='utf-8')
        print(f'response written to {target}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
