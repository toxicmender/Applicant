"""Evaluate a frozen language evidence set against an immutable profile."""

from collections import defaultdict
from decimal import Decimal, localcontext

from .language import LEVELS, LanguageExpression
from .ranking_math import arithmetic_context
from .research import ResearchEvidence


def role_evidence(job):
    return tuple(
        ResearchEvidence.model_validate(item)
        for evidence in job.evidence
        if evidence.payload.get('kind') == 'research_language_context'
        for item in evidence.payload['assertions']
    )


def compare(candidate, skill, threshold):
    if candidate is None or skill is None:
        return 'unknown', 'skill_missing', None
    ability = getattr(candidate, skill)
    if candidate.none or (ability is not None and ability.ability == 'none'):
        return 'fail', 'no_proficiency', ability
    if ability is None:
        return 'unknown', 'skill_missing', None
    if threshold is None or threshold.kind == 'raw':
        return 'unknown', 'threshold_unknown', ability
    if threshold.kind == 'no_minimum':
        if ability.level is not None or ability.ability == 'yes':
            return 'pass', 'ability_confirmed', ability
        return 'unknown', 'ability_unknown', ability
    if ability.level is None:
        return 'unknown', 'level_missing', ability
    if ability.level.scale != threshold.scale:
        return 'unknown', 'scale_incomparable', ability
    assert threshold.scale is not None and threshold.level is not None
    assert ability.level.level is not None
    levels = LEVELS[threshold.scale]
    if levels.index(ability.level.level) >= levels.index(threshold.level):
        return 'pass', 'proficiency_met', ability
    return 'fail', 'proficiency_below_minimum', ability


def combine(operator, statuses):
    decisive, unanimous = ('fail', 'pass') if operator == 'all_of' else ('pass', 'fail')
    if decisive in statuses:
        return decisive
    if statuses and all(status == unanimous for status in statuses):
        return unanimous
    return 'conflict' if 'conflict' in statuses else 'unknown'


def expression_status(expression, members):
    if isinstance(expression, str):
        return members.get(expression, 'unknown')
    return combine(
        expression.operator,
        [expression_status(member, members) for member in expression.members],
    )


def evaluate_language(job, profile):
    clauses = defaultdict(lambda: defaultdict(list))
    for evidence in role_evidence(job):
        fact = evidence.payload
        if fact.kind != 'language':
            raise ValueError('frozen language evidence must contain a language assertion')
        clauses[fact.clause_id][fact.member_id].append(evidence)
    candidates = {item.language: item for item in getattr(profile, 'languages', ())}
    groups = []
    for clause, members in sorted(clauses.items()):
        strengths = set()
        expressions = {}
        breakdown = []
        for member, assertions in sorted(members.items()):
            facts = [assertion.payload for assertion in assertions]
            values = {
                field: {getattr(fact, field) for fact in facts if getattr(fact, field) is not None}
                for field in ('language', 'skill', 'strength', 'threshold')
            }
            strengths.update(values['strength'])
            for fact in facts:
                if fact.expression:
                    expressions[fact.expression.model_dump_json()] = fact.expression
            effective = {
                field: next(iter(items)) if len(items) == 1 else None
                for field, items in values.items()
            }
            if any(len(items) > 1 for items in values.values()):
                status, reason, ability = 'conflict', 'language_assertion_conflict', None
            elif any(fact.interpretation_review for fact in facts):
                status, reason, ability = 'unknown', 'interpretation_review', None
            elif effective['strength'] == 'not_required':
                status, reason, ability = 'not_applicable', 'language_not_required', None
            else:
                status, reason, ability = compare(
                    candidates.get(effective['language']),
                    effective['skill'],
                    effective['threshold'],
                )
            threshold = effective['threshold']
            breakdown.append(
                {
                    'member_id': member,
                    'language': effective['language'],
                    'skill': effective['skill'],
                    'status': status,
                    'reason': reason,
                    'required_threshold': threshold.model_dump(mode='json') if threshold else None,
                    'candidate_ability': ability.model_dump(mode='json') if ability else None,
                    'evidence_ids': sorted(item.evidence_id for item in assertions),
                    'excerpts': [
                        item.excerpt for item in sorted(assertions, key=lambda e: e.evidence_id)
                    ],
                }
            )
        statuses = {
            item['member_id']: item['status']
            for item in breakdown
            if item['status'] != 'not_applicable'
        }
        if 'required' in strengths and strengths != {'required'}:
            strength, status, reason = 'required', 'conflict', 'mandatory_status_conflict'
        elif len(expressions) > 1:
            strength = 'preferred' if strengths == {'preferred'} else 'required'
            status, reason = 'conflict', 'language_expression_conflict'
        else:
            strength = next(iter(strengths)) if len(strengths) == 1 else 'required'
            expression: LanguageExpression | None = next(iter(expressions.values()), None)
            status = (
                expression_status(expression, statuses)
                if expression
                else combine('all_of', tuple(statuses.values()))
                if statuses
                else 'not_applicable'
            )
            reason = 'language_group_' + status
        groups.append(
            {
                'clause_id': clause,
                'strength': strength,
                'status': status,
                'reason': reason,
                'members': breakdown,
            }
        )
    return tuple(groups)


def preferred_score(groups):
    statuses = [group['status'] for group in groups if group['strength'] == 'preferred']
    passed, failed, conflicted = (statuses.count(state) for state in ('pass', 'fail', 'conflict'))
    denominator = passed + failed + conflicted
    with localcontext(arithmetic_context()):
        return (
            Decimal(100) * passed / denominator if denominator else None,
            Decimal(passed + failed) / len(statuses) if statuses else Decimal(0),
        )
