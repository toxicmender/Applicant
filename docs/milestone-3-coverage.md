# Milestone 3 delivery and coverage audit

Validated locally on Windows, 2026-11-03, on `codex/ranking-foundation` including
`330d62c`. All six implementation phases are delivered locally. Hosted CI remains
unverified; this audit does not claim Linux execution or installed Chromium.

## Validation results

- Full branch-enabled suite: **1,158 passed, 13 skipped, 1,176 subtests passed** in
  95.55 seconds. Nine skips need Chromium; four require POSIX behavior.
- Overall coverage reports **99%** (rounded), above the unchanged 90% floor.
- The 12 M3 modules below have **100% statement and branch coverage**, combined
  **596 statements and 174 branches**. No exclusions were added.
- Ruff, formatting, Pyright for Windows and Linux targets, uv lock --check and
  uv build passed.
- The repeatable CLI journey passed both in a locked, isolated non-editable
  project install and against the built `dist/applicant-0.2.0-py3-none-any.whl` in
  an isolated offline environment.

| Area | Audited modules | Coverage |
| --- | --- | --- |
| CLI dispatch/options/output | cli/rank.py, cli/survey.py | 100% |
| Editable/input/report contracts | domain/survey.py, domain/ranking_inputs.py, domain/ranking_report.py | 100% |
| Strict document/listing reads | infra/preference_documents.py, infra/ranking_documents.py, infra/store/listing_snapshots.py | 100% |
| Survey workflows and interaction | services/survey.py, services/survey_questionnaire.py | 100% |
| Ranking preflight and historical reads | services/ranking_cli.py, services/ranking_read.py | 100% |

Existing profile and completed-run repositories also have 100% coverage. The
existing SQLite module is 97%: remaining gaps include foreign-key/WAL availability
guards, sidecar-presence behavior and older application/financial import branches.
Its new read APIs have no uncovered lines. The v2 ranking contracts remain 99%,
with the defensive nonfinite calculation-term check at line 229 unreachable after
Pydantic rejects nonfinite Decimal fields. These gaps were retained and disclosed;
validators were not bypassed to increase the percentage.

The subprocess journey is a behavior/packaging check. Child-process execution is
not merged into coverage by the current configuration; the module measurements
come from the ordinary in-process unit/integration tests.

## Repeatable end-to-end evidence

`tools/smoke_ranking.py` uses separate CLI processes, temporary data in a path with
spaces, Unicode and `#`, and a clean Applicant settings environment. It leaves no
database in the checkout and makes no provider calls. Its golden inputs are
`tests/fixtures/ranking/cli-versions.json`, `preferences.json`, `provenance.json`
and `cli-journey.json`.

The journey validates and dry-runs a profile without creating a database, imports
it, ranks one eligible/review/excluded job each, verifies the known score/reasons,
and checks that the conflict explanation retains two witnesses and a verification
prompt. It then edits the profile, changes the listing and scoring configuration,
creates another run, and confirms the original report and replay still match.
Historical operations preserve database bytes. Synthetic fixture provenance is
explicitly supplied; this tests identifier comparison, not binary attestation.

The broader suite covers stale/concurrent profile edits, cancellation/EOF/interrupt,
group correction, Decimal context isolation, strict JSON before writes, archived
schema dispatch, raw JSON number/hash preservation, tier selections, display-only
top limits, terminal escaping, error codes, corruption recovery and legacy output.
Existing route predicate-count and property tests continue to prevent Cartesian
route evaluation; CLI tests assert a single engine evaluation per new batch.

## CI integration and reproduction

The existing gated unit matrix now runs the installed journey with its explicit
Python version: Linux 3.10–3.13 and Windows 3.12. It uses frozen dependencies and
an isolated non-editable install. The helper refuses an editable source import.
Workflow tests verify that the step is unconditional, does not ignore errors,
and references a tracked script. The final status gate reads the entire unit
job result, so a failed installed journey cannot be masked by passing pytest.

```console
uv run --frozen coverage run -m pytest tests -q
uv run --frozen coverage report
uv run --frozen --isolated --no-editable python tools/smoke_ranking.py --require-installed
uv lock --check
uv build
uv run --no-project --isolated --offline --with dist/applicant-0.2.0-py3-none-any.whl python tools/smoke_ranking.py --require-installed
```

The final command needs the wheel and its dependencies in uv's cache; omit
--offline if they must be downloaded. No project dependencies were added.
CI configuration is committed; hosted Linux/Windows/browser results require a
future pushed workflow run. Region filtering, posting refresh, feedback and
learned weights remain the explicit deferrals in the implementation plans.
