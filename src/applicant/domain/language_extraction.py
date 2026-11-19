"""Conservative extraction from the text of a known posting."""

import re
from html import unescape
from typing import Any

from .canonical import content_hash
from .language import ALIASES, LEVELS, LanguageAssertion, LanguageExpression, Skill, Threshold

LANGUAGES = '|'.join(map(re.escape, ALIASES))
LANGUAGE_NAME = re.compile(rf'\b({LANGUAGES})\b', re.I)
STRENGTH = re.compile(
    r'\b(not (?:strictly )?required|not a dealbreaker|not needed|optional|'
    r'mandatory|required|must|preferred|desirable|nice to have)\b',
    re.I,
)
CEFR = re.compile(r'\b(A1|A2|B1|B2|C1|C2)\b', re.I)
NAMED = re.compile(r'\b(' + '|'.join(LEVELS['named']) + r')\b', re.I)
AMBIGUITY = re.compile(r'\b(where|if|unless|equivalent|depending|prohibited|must not)\b', re.I)


def extract_language(text: str | None) -> tuple[dict[str, Any], ...]:
    if not text:
        return ()
    plain = unescape(re.sub(r'<[^>]+>', '\n', text))
    results = []
    for match in re.finditer(r'[^.!?;\n]+', plain):
        clause = ' '.join(match[0].split())
        names = list(LANGUAGE_NAME.finditer(clause))
        qualifiers = list(STRENGTH.finditer(clause))
        if not names or not qualifiers:
            continue
        tokens = {word[0].lower() for word in qualifiers}
        required = bool(tokens & {'required', 'mandatory', 'must'})
        preferred = bool(tokens & {'preferred', 'desirable', 'nice to have'})
        strength = 'required' if required else 'preferred' if preferred else 'not_required'
        directional: tuple[Skill, ...] = ('spoken', 'written')
        skills: list[Skill] = [
            skill for skill in directional if re.search(rf'\b{skill}\b', clause, re.I)
        ]
        if not skills:
            skills.append('overall')
        thresholds = CEFR.findall(clause)
        named = NAMED.findall(clause)
        threshold = Threshold(kind='no_minimum')
        if len(thresholds) == 1:
            threshold = Threshold(kind='level', scale='cefr', level=thresholds[0].upper())
        elif len(named) == 1 and not any(word.startswith('not ') for word in tokens):
            threshold = Threshold(kind='level', scale='named', level=named[0].lower())
        elif re.search(r'\b(?:native.level|ILR\s+\d)\b', clause, re.I):
            threshold = Threshold(kind='raw', level=clause)
        clause_id = content_hash((match.start(), clause))
        members: list[tuple[str, Skill, str]] = [
            (name[0], skill, f'{clause_id}-{name.start()}-{skill}')
            for name in names
            for skill in skills
        ]
        expression = None
        if len(members) > 1:
            by_language = [
                tuple(member_id for _, _, member_id in members[index : index + len(skills)])
                for index in range(0, len(members), len(skills))
            ]
            expression = LanguageExpression(
                operator='any_of' if re.search(r'\bor\b', clause, re.I) else 'all_of',
                members=tuple(
                    group[0]
                    if len(group) == 1
                    else LanguageExpression(operator='all_of', members=group)
                    for group in by_language
                ),
            )
        for language, skill, member_id in members:
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
                    or (required and preferred)
                    or len(thresholds) > 1
                    or len(named) > 1
                ),
            )
            results.append({'excerpt': clause, 'payload': fact.model_dump(mode='json')})
    return tuple(results)
