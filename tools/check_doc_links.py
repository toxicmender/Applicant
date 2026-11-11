"""Check repository documentation links and GitHub-style section anchors."""

from __future__ import annotations

import argparse
import re
import subprocess
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from urllib.parse import unquote, urlsplit

FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})(.*)$')
HEADING = re.compile(r'^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$')
ANCHOR = re.compile(r"""<\w+\b[^>]*?\s(?:id|name)=(["'])(.*?)\1""", re.I)
DESTINATION = r'(<[^>]+>|(?:[^\s()]+|\([^()]*\))+)'
INLINE = re.compile(r'\]\(\s*' + DESTINATION + r'\s*(?:["\'][^\n]*?["\']\s*)?\)')
REFERENCE = re.compile(r'^ {0,3}\[[^\]]+\]:\s*' + DESTINATION, re.M)
HTML_LINK = re.compile(r"""<(?:a|img)\b[^>]*?\s(?:href|src)=(["'])(.*?)\1""", re.I)


@dataclass(frozen=True)
class Document:
    anchors: frozenset[str]
    collisions: tuple[str, ...]
    targets: frozenset[str]


def read_document(markdown: str) -> Document:
    lines = []
    fence = ''
    for line in markdown.splitlines():
        marker = FENCE.match(line)
        if fence:
            if marker and marker[1].startswith(fence) and not marker[2].strip():
                fence = ''
            continue
        if marker:
            fence = marker[1]
        else:
            lines.append(line)

    prose = re.sub(r'<!--.*?-->', '', '\n'.join(lines), flags=re.S)
    headings: set[str] = set()
    explicit: set[str] = set()
    collisions: set[str] = set()
    for line in prose.splitlines():
        heading = HEADING.match(line)
        if heading:
            title = re.sub(r'!?(\[[^\]]+\])\([^)]*\)', r'\1', heading[1])
            title = re.sub(r'<[^>]*>', '', title)
            title = re.sub(r'(?<!\w)_([^_]+)_(?!\w)', r'\1', title)
            slug = re.sub(r'[^\w -]', '', unescape(title).lower()).replace(' ', '-')
            unique = slug
            suffix = 1
            while unique in headings:
                unique = f'{slug}-{suffix}'
                suffix += 1
            headings.add(unique)
        for anchor in ANCHOR.finditer(line):
            name = unescape(anchor[2])
            if name in explicit:
                collisions.add(name)
            explicit.add(name)

    prose = re.sub(r'(`+).*?\1', '', prose)
    targets = {unescape(match[1].strip('<>')) for match in INLINE.finditer(prose)}
    targets.update(unescape(match[1].strip('<>')) for match in REFERENCE.finditer(prose))
    targets.update(unescape(match[2]) for match in HTML_LINK.finditer(prose))
    return Document(
        frozenset(headings | explicit),
        tuple(sorted(collisions | (headings & explicit))),
        frozenset(targets),
    )


def check_links(root: Path) -> tuple[list[str], set[str]]:
    paths = [root / 'README.md', *sorted((root / 'docs').rglob('*.md'))]
    documents = {path: read_document(path.read_text(encoding='utf-8')) for path in paths}
    errors = []
    external: set[str] = set()
    for path, document in documents.copy().items():
        label = path.relative_to(root).as_posix()
        errors.extend(f'{label}: duplicate anchor #{name}' for name in document.collisions)
        for target in sorted(document.targets):
            parts = urlsplit(target)
            if parts.scheme or parts.netloc:
                external.add(target)
                continue
            link_path = unquote(parts.path)
            if link_path.startswith('/'):
                destination = (root / link_path.lstrip('/')).resolve()
            else:
                destination = (path.parent / link_path).resolve() if link_path else path
            if not destination.exists():
                errors.append(f'{label}: missing target {target}')
            elif parts.fragment and destination.suffix.lower() == '.md':
                if destination not in documents:
                    documents[destination] = read_document(destination.read_text(encoding='utf-8'))
                if unquote(parts.fragment) not in documents[destination].anchors:
                    errors.append(f'{label}: missing fragment {target}')
    return errors, external


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--legacy-readme-ref', help='Git revision whose README anchors must survive'
    )
    parser.add_argument(
        '--list-external', action='store_true', help='list external URLs separately'
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    errors, external = check_links(root)
    if args.legacy_readme_ref:
        original = subprocess.check_output(
            ['git', 'show', f'{args.legacy_readme_ref}:README.md'], cwd=root, encoding='utf-8'
        )
        current = read_document((root / 'README.md').read_text(encoding='utf-8'))
        missing = read_document(original).anchors - current.anchors
        errors.extend(f'README.md: missing legacy anchor #{name}' for name in sorted(missing))
    if args.list_external:
        print('External URLs (listed only)')
        print('\n'.join(sorted(external)))
    if errors:
        print('\n'.join(errors))
        return 1
    print('Documentation links and anchors passed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
