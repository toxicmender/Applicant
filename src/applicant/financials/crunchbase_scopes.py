"""Attribute browser financial cards to the requested Crunchbase organization."""

import hashlib
import logging
from collections.abc import Iterator

from ..errors import Unparseable

logger = logging.getLogger(__name__)

_CARDS = ('fields', 'raised_funding_rounds')
_WRAPPERS = {'data', 'entity', 'entities', 'results', 'cards', 'properties', *_CARDS}
_REFERENCES = {
    'identifier',
    'relationships',
    'funded_organization_identifier',
    'investors',
    'investor',
    'lead_investor_identifiers',
    'raised_funding_rounds',
}


def _mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def _declarations(node: dict) -> list[dict]:
    fields = _mapping(_mapping(node.get('cards')).get('fields'))
    result = []
    for owner in (node, _mapping(node.get('properties')), fields):
        identifier = _mapping(owner.get('identifier'))
        kind = identifier.get('entity_def_id')
        # Round and investor identifiers are never implicit organizations.
        if kind != 'organization' and (
            kind is not None
            or 'entity_def_id' in identifier
            or 'investment_type' in owner
            or 'funded_organization_identifier' in owner
        ):
            continue
        slug = identifier.get('permalink')
        if isinstance(slug, str) and slug:
            result.append(identifier)
    return result


def _child_path(path: str, key: str) -> str:
    if key in _WRAPPERS:
        return f'{path}.{key}'
    opaque = hashlib.sha256(key.encode('utf-8', errors='replace')).hexdigest()[:12]
    return f'{path}.[{opaque}]'


def warn(index: int, path: str, reason: str) -> None:
    logger.warning('crunchbase: %s at payload[%d]%s', reason, index, path)


def _nodes(value, path='$', *, primary=True, key=None) -> Iterator[tuple]:
    if isinstance(value, list):
        for index, child in enumerate(value):
            yield from _nodes(child, f'{path}[{index}]', primary=False)
    elif isinstance(value, dict):
        declarations = _declarations(value)
        yield value, path, primary, key, declarations
        if declarations or 'investment_type' in value:
            return
        for name, child in value.items():
            if name in _REFERENCES:
                continue
            yield from _nodes(
                child,
                _child_path(path, name),
                primary=primary and name in ('data', 'entity'),
                key=name,
            )


def _scalars(node: dict) -> dict:
    return {
        name: value
        for name, value in node.items()
        if name not in ('properties', 'cards', 'raised_funding_rounds', 'relationships')
    }


def _components(node: dict) -> Iterator[tuple[str, str, object]]:
    yield 'organization', '', _scalars(node)
    properties = node.get('properties')
    if properties is not None:
        yield 'organization', '.properties', properties
    cards = node.get('cards')
    if cards is not None and not isinstance(cards, dict):
        yield 'invalid', '.cards', cards
    fields = _mapping(cards).get('fields')
    if fields is not None:
        yield 'organization', '.cards.fields', fields
    if 'fields' in node:
        yield 'organization', '.fields', node['fields']
    for owner, prefix in ((node, ''), (_mapping(cards), '.cards')):
        if 'raised_funding_rounds' in owner:
            yield 'rounds', f'{prefix}.raised_funding_rounds', owner['raised_funding_rounds']


def verified_components(payload, target: str, index: int) -> Iterator[tuple[str, str, object]]:
    """Yield bounded, explicitly associated components and degraded markers."""
    nodes = list(_nodes(payload))
    uuid_slugs: dict[str, set[str]] = {}
    for _, _, _, _, declarations in nodes:
        for identifier in declarations:
            uuid = identifier.get('uuid')
            if isinstance(uuid, str) and uuid:
                uuid_slugs.setdefault(uuid, set()).add(identifier['permalink'])

    for node, path, primary, key, declarations in nodes:
        slugs = {identifier['permalink'] for identifier in declarations}
        if len(slugs) > 1 and target in slugs:
            if primary:
                raise Unparseable('contradictory primary Crunchbase organization')
            warn(index, path, 'conflicting_organization')
            yield 'degraded', path, None
            continue
        if declarations and slugs != {target}:
            continue

        keyed = key == target or (key is not None and uuid_slugs.get(key) == {target})
        if key is not None and target in uuid_slugs.get(key, ()) and not keyed:
            if any(name in node for name in (*_CARDS, 'cards', 'properties')):
                warn(index, path, 'conflicting_organization_uuid')
                yield 'degraded', path, None
            continue
        if not declarations and (not keyed or not any(name in node for name in ('cards', *_CARDS))):
            continue
        if declarations:
            yield 'verified', path, None
        # Descendant nodes of generic keyed envelopes can be encountered later,
        # but only the keyed envelope owns these cards.
        for kind, suffix, component in _components(node):
            yield kind, path + suffix, component
