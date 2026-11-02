# Ranking scoring coverage review

Validated 2026-11-02 on `codex/ranking-foundation`, including scoring commit
`0589ac2` and the subsequent ranked-view/coverage-audit slice.

## Results

The full Windows suite reports **1,095 passed, 13 skipped, 1,041 subtests passed**
in 79.25 seconds under coverage. Overall branch-enabled project coverage is **98%**,
above the existing 90% gate. Nine skips require unavailable Chromium; four require
POSIX behavior. Linux-target Pyright passed; hosted Linux tests are not claimed.

| Audited production area | Statement and branch coverage | Evidence |
| --- | --- | --- |
| New dimension scoring, aggregation, ranked views and rating capture | 100% combined: 246 statements, 108 branches | Golden arithmetic, boundaries, count/age/source policy, context isolation, permutation properties, duplicate rejection and immutable evidence capture |
| Existing v2 ranking contracts | 99%: 220 statements, 72 branches, one missed statement/branch | Added contradictory score/exclusion states, duplicate criteria/jobs, normalization reference mismatch and conflict-identity rejection |
| Existing completed-run repository | 100% | Added corrupt metadata and SQL-key/result-payload mismatch cases, supplementing existing completion/recovery tests |

The remaining v2 contract line is the explicit nonfinite calculation-term guard.
Pydantic's Decimal field validation rejects those values before the model-level
guard runs. Existing nonfinite-input tests assert rejection. The review retained
the defensive check and did not bypass validation or weaken coverage policy to
exercise an unreachable path.

## What the tests establish

- The salary utility curve matches independently specified golden endpoints and
  piecewise values. Increasing a known score does not reduce aggregate fit.
- Fit remains unchanged when only evidence support changes. Zero-weight and
  inapplicable dimensions leave both denominators; unknowns remain in the evidence
  denominator. Known zero and missing scores stay distinct.
- Skill aliases deduplicate exact canonical tokens without substring matching.
  Incomplete/conflicting requirements remain unknown.
- Rating selection respects configured provider priority, recency and stable
  hashes; freshness includes the exact elapsed-day boundary. Review-count support,
  missing counts, unsupported categories/providers and ambiguous company IDs are
  exercised. Overall ratings never fill absent culture/stability categories.
- Ranked views partition eligibility states, place unknown scores last, and break
  displayed-score ties by job ID. Confidence is not an implicit tie breaker.
- Complete raw and typed company evidence is isolated from later caller mutation.
  Captured inputs and computed scores/confidence survive reopening and reproduce
  without network access.
- History recovery rejects malformed metadata objects, inconsistent result keys,
  and invalid contract states rather than returning plausible-looking rankings.

The tests do not establish empirical recommendation quality or calibrated
probabilities. Live-provider quality, hosted CI and complete engine replay remain
separate acceptance work. The installed wheel passed an isolated offline
parse/normalize/eligibility/score/view journey.

## Reproduce

```console
uv run --frozen coverage run -m pytest tests -q
uv run --frozen coverage report
uv run --frozen coverage report --include="*/domain/dimension_scoring.py,*/domain/scoring.py,*/domain/ranking_views.py,*/services/ranking_evidence.py"
```

Focused tests live in `tests/test_scoring.py`, `tests/test_scoring_history.py`,
`tests/test_ranking_views.py`, `tests/test_ranking_evidence.py`,
`tests/test_ranking_contract_boundaries.py` and
`tests/test_ranking_recovery_integrity.py`. Golden cases are in
`tests/fixtures/ranking/scoring.json`.
