"""Scriptable questionnaire; no database connection is held while asking."""

from decimal import localcontext

from pydantic import TypeAdapter

from ..domain.canonical import canonical_json
from ..domain.contracts import Fraction
from ..domain.preferences import DIMENSIONS
from ..domain.ranking_math import arithmetic_context
from ..domain.ranking_v2 import CandidateProfileV2
from ..domain.survey import PreferenceDocument
from ..interaction import Interaction

GROUPS = (
    (
        ('experience_years', 'Experience in years (0 or more)'),
        ('primary_discipline', 'Primary engineering discipline'),
    ),
    (('target_seniority', 'Target seniority (Junior, Mid, Senior, Staff, Principal)'),),
    (('core_technologies', 'Core technologies (comma-separated names)'),),
    (('preferred_regions', 'Preferred regions (retained; currently not scored)'),),
    (('preferred_work_modes', 'Preferred modes (Remote, Hybrid, Onsite; soft fit)'),),
    (('timezone_overlap_tolerance', 'Timezone overlap (retained; currently not scored)'),),
    (('work_country', 'Actual work country (ISO alpha-2, e.g. IN; eligibility)'),),
    (('required_work_modes', 'Required modes (Remote, Hybrid, Onsite; eligibility)'),),
    (
        ('minimum_base_salary', 'Minimum annual base salary (hard floor)'),
        ('preferred_base_salary', 'Preferred annual base salary (soft target)'),
        ('salary_currency', 'Salary currency (ISO alpha-3, e.g. EUR)'),
        ('salary_basis', 'Salary basis (gross, net; no tax conversion)'),
    ),
    (('employment_preference', 'Employment (permanent_only, contract_only, either)'),),
    tuple(
        (f'weights.{name}', f'{name} weight (fraction 0..1; all six must sum to 1)')
        for name in DIMENSIONS
    ),
)
LIST_FIELDS = frozenset(
    {
        'target_seniority',
        'core_technologies',
        'preferred_regions',
        'preferred_work_modes',
        'required_work_modes',
    }
)
OPTIONAL_FIELDS = frozenset(
    {
        'timezone_overlap_tolerance',
        'work_country',
        'minimum_base_salary',
        'preferred_base_salary',
    }
)
INTRO = (
    'Candidate survey. Enter keeps the displayed value; - clears optional fields/lists.\n'
    'Lists use commas. Salary is annual base pay; weights are fractions, not percentages.\n'
    'Required modes constrain eligibility; preferred modes affect fit. Country is where you work.\n'
    'Experience, discipline, seniority, regions and timezone are retained without current scoring signals.\n'
)


def _ask(interaction: Interaction, name: str, label: str, values: dict, notice: str) -> None:
    weight = name.startswith('weights.')
    key = name.split('.')[-1]
    target = values['weights'] if weight else values
    adapter = TypeAdapter(
        Fraction if weight else CandidateProfileV2.model_fields[name].rebuild_annotation()
    )
    while True:
        shown = canonical_json(target[key]) if key in target else 'required'
        answer = interaction.ask(f'{notice}{label} [{shown}]: ').strip()
        try:
            if not answer:
                if key not in target:
                    raise ValueError('a value is required')
                raw = target[key]
            elif answer == '-':
                if name not in LIST_FIELDS and name not in OPTIONAL_FIELDS:
                    raise ValueError('this field cannot be cleared')
                raw = () if name in LIST_FIELDS else None
            elif name in LIST_FIELDS:
                items = tuple(part.strip() for part in answer.split(','))
                if not all(items):
                    raise ValueError('list items must not be empty')
                raw = tuple(dict.fromkeys(items))
            else:
                raw = answer
            with localcontext(arithmetic_context()):
                target[key] = adapter.validate_python(raw)
            return
        except ValueError as error:
            notice = f'Invalid value: {error}\n'


def questionnaire(
    interaction: Interaction, *, profile_id: str, initial: PreferenceDocument | None = None
) -> PreferenceDocument | None:
    """Collect all fields and return confirmed edits, or None on decline/EOF.

    Reuse the profile's field and cross-field validators. Invalid salary or weight
    groups are asked again without discarding the other answers or changing CAS.
    KeyboardInterrupt propagates to the CLI's existing interrupt handler.
    """
    if initial is None:
        seed = PreferenceDocument(
            profile_id=profile_id,
            expected_revision=0,
            preferences={'experience_years': 0, 'primary_discipline': 'seed'},
        )
        values = seed.expanded()
        del values['experience_years'], values['primary_discipline']
        revision = 0
    else:
        values = initial.expanded()
        profile_id = initial.profile_id
        revision = initial.expected_revision
    notice = INTRO
    try:
        for group in GROUPS:
            while True:
                for name, label in group:
                    _ask(interaction, name, label, values, notice)
                    notice = ''
                try:
                    document = PreferenceDocument(
                        profile_id=profile_id, expected_revision=revision, preferences=values
                    )
                    break
                except ValueError as error:
                    notice = f'Invalid group: {error}\nPlease correct this group.\n'
        summary = canonical_json(document.model_dump() | {'preferences': document.expanded()})
        question = f'Proposed preferences (next revision {revision + 1}):\n{summary}\nSave? [y/N]: '
        while True:
            answer = interaction.ask(question).strip().casefold()
            if answer in ('y', 'yes'):
                return document
            if answer in ('', 'n', 'no'):
                return None
            question = 'Please answer yes or no. Save? [y/N]: '
    except EOFError:
        return None
