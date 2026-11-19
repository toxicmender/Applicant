"""Conservative extraction from the text of a known posting."""

import re
from html import unescape
from itertools import pairwise
from typing import Any

from .canonical import content_hash
from .language import ALIASES, LEVELS, LanguageAssertion, LanguageExpression, Skill, Threshold

LANGUAGES = '|'.join(map(re.escape, ALIASES))
LANGUAGE_NAME = re.compile(rf'\b({LANGUAGES})\b', re.I)
STRENGTH = re.compile(
    r'\b(not (?:strictly )?required|not a dealbreaker|not needed|optional|'
    r'mandatory|required|must not|must|preferred|desirable|nice to have|a plus|helpful)\b',
    re.I,
)
CEFR = re.compile(r'\b(A1|A2|B1|B2|C1|C2)\b', re.I)
NAMED = re.compile(r'\b(' + '|'.join(LEVELS['named']) + r')\b', re.I)
AMBIGUITY = re.compile(r'\b(where|if|unless|equivalent|depending|prohibited|must not)\b', re.I)
HEDGE = re.compile(
    r'\b(?:though|but|where|if|depending)\b.*\b(?:may |might |can )?require it\b', re.I
)
CLAUSE_JOIN = re.compile(r'\s+(?:and|but)\s+', re.I)
MEMBER_JOIN = re.compile(r'\b(and|or)\b', re.I)
SKILL_JOIN = re.compile(r'\s+and\s+(?=(?:spoken|written)\b)', re.I)
MODIFIER = re.compile(
    r'\b(?:is|are|be|also|both|at least|minimum|level|skills?|proficiency|'
    r'fluency|speak|speaking|spoken|write|written|reading|writing|have|knowledge|of|in|prior|'
    r'language|ability|command|no|strongly|strictly|or equivalent|and|or)\b',
    re.I,
)
REQUIRED = {'required', 'mandatory', 'must', 'must not'}
PREFERRED = {'preferred', 'desirable', 'nice to have', 'a plus', 'helpful'}


def _clauses(plain: str):
    for sentence in re.finditer(r'[^.!?;\n]+', plain):
        start = 0
        text = sentence[0]
        boundaries = sorted(
            [*CLAUSE_JOIN.finditer(text), *re.finditer(r',\s*', text)], key=lambda m: m.start()
        )
        for boundary in boundaries:
            left = text[start : boundary.start()]
            right = text[boundary.end() :]
            if LANGUAGE_NAME.search(left) and HEDGE.match(right):
                continue
            if boundary[0].startswith(',') or (STRENGTH.search(left) and STRENGTH.search(right)):
                yield sentence.start() + start, left
                start = boundary.end()
        yield sentence.start() + start, text[start:]


def _attached(clause, name, qualifier) -> bool:
    if name.end() <= qualifier.start():
        gap = clause[name.end() : qualifier.start()]
    else:
        gap = clause[qualifier.end() : name.start()]
        # A requirement on the left of a conjunction cannot qualify the next subject.
        if re.match(r'\s*(?:and|but|or)\b', gap, re.I):
            return False
    for pattern in (LANGUAGE_NAME, CEFR, NAMED, MODIFIER):
        gap = pattern.sub(' ', gap)
    gap = re.sub(r'\b(?:native.level|ILR\s+\d)\b', ' ', gap, flags=re.I)
    return not gap.strip(' (),:-')


def _threshold(text: str, *, negated: bool = False) -> tuple[Threshold, bool]:
    levels = {level.upper() for level in CEFR.findall(text)}
    named = {level.lower() for level in NAMED.findall(text)}
    if len(levels) + len(named) > 1:
        return Threshold(kind='raw', level=text), True
    if levels and not negated:
        return Threshold(kind='level', scale='cefr', level=next(iter(levels))), False
    if named and not negated:
        return Threshold(kind='level', scale='named', level=next(iter(named))), False
    if re.search(r'\b(?:native.level|ILR\s+\d)\b', text, re.I):
        return Threshold(kind='raw', level=text), False
    return Threshold(kind='no_minimum'), False


def _member_scopes(clause: str, names: list[re.Match[str]]) -> tuple[list[str], set[str]]:
    boundaries = []
    for first, second in pairwise(names):
        joins = list(MEMBER_JOIN.finditer(clause, first.end(), second.start()))
        if not joins:
            return [clause] * len(names), set()
        boundaries.append(joins[-1])
    scopes = [
        clause[start:end]
        for start, end in zip(
            [0, *(join.end() for join in boundaries)],
            [*(join.start() for join in boundaries), len(clause)],
            strict=True,
        )
    ]
    return scopes, {join[0].lower() for join in boundaries}


def _skill_scopes(scope: str) -> list[tuple[Skill, str]]:
    directional: tuple[Skill, ...] = ('spoken', 'written')
    skills: list[Skill] = [
        skill for skill in directional if re.search(rf'\b{skill}\b', scope, re.I)
    ]
    if not skills:
        return [('overall', scope)]
    parts = SKILL_JOIN.split(scope, maxsplit=1)
    if len(skills) == len(parts) == 2 and all(
        CEFR.search(part) or NAMED.search(part) for part in parts
    ):
        return [
            (skill, next((part for part in parts if re.search(rf'\b{skill}\b', part, re.I)), scope))
            for skill in skills
        ]
    return [(skill, scope) for skill in skills]


def extract_language(text: str | None) -> tuple[dict[str, Any], ...]:
    if not text:
        return ()
    plain = unescape(re.sub(r'<[^>]+>', '\n', text))
    results = []
    for offset, excerpt in _clauses(plain):
        clause = ' '.join(excerpt.split())
        qualifiers = list(STRENGTH.finditer(clause))
        linked = [
            (name, [word[0].lower() for word in qualifiers if _attached(clause, name, word)])
            for name in LANGUAGE_NAME.finditer(clause)
        ]
        names = [name for name, words in linked if words]
        if not names:
            continue
        tokens = {word for _, words in linked for word in words}
        hedged = bool(HEDGE.search(clause))
        negated = bool(re.search(r'\bno\s+(?:prior\s+)?(?:' + LANGUAGES + r')\b', clause, re.I))
        required = hedged or (bool(tokens & REQUIRED) and not negated)
        preferred = bool(tokens & PREFERRED)
        strength = 'required' if required else 'preferred' if preferred else 'not_required'
        scopes, operators = _member_scopes(clause, names)
        clause_id = content_hash((offset, clause))
        members = []
        groups: list[str | LanguageExpression] = []
        for name, scope in zip(names, scopes, strict=True):
            ids = []
            for skill, level_text in _skill_scopes(scope):
                member_id = f'{clause_id}-{name.start()}-{skill}'
                members.append((name[0], skill, member_id, level_text))
                ids.append(member_id)
            groups.append(
                ids[0]
                if len(ids) == 1
                else LanguageExpression(operator='all_of', members=tuple(ids))
            )
        if len(groups) > 1:
            expression = LanguageExpression(
                operator='any_of' if operators == {'or'} else 'all_of',
                members=tuple(groups),
            )
        else:
            expression = groups[0] if isinstance(groups[0], LanguageExpression) else None
        for language, skill, member_id, level_text in members:
            threshold, ambiguous_level = _threshold(level_text, negated=strength == 'not_required')
            fact = LanguageAssertion(
                clause_id=clause_id,
                member_id=member_id,
                language=language,
                skill=skill,
                strength=strength,
                threshold=threshold,
                expression=expression,
                interpretation_review=bool(
                    AMBIGUITY.search(clause)
                    or hedged
                    or len(operators) > 1
                    or (required and preferred)
                    or ambiguous_level
                ),
            )
            results.append({'excerpt': clause, 'payload': fact.model_dump(mode='json')})
    return tuple(results)
