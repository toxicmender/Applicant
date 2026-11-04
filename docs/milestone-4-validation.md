# Milestone 4 validation and CI gates

Phase 9 connects the delivered M4 capabilities through real CLI subprocesses and
adds required installed-package and per-module coverage gates. It introduces no
runtime dependency, database migration, ranking-policy or contract-format change.

## Reproduce locally

```console
uv sync --locked
uv run --frozen coverage run -m pytest tests -q
uv run --frozen coverage report
uv run --frozen coverage json -o coverage-m4.json
uv run --frozen python tools/check_milestone_coverage.py coverage-m4.json
uv run --frozen --isolated --no-editable --refresh-package applicant --reinstall-package applicant python tools/smoke_milestone4.py --require-installed
```

The explicit refresh/reinstall prevents reuse of an older locally built wheel
with the same project version. The installed check rejects an editable source
import. The script, golden fixtures and coverage manifest live in the checkout;
application code comes from the isolated installed package.

`coverage-m4.json` and `milestone-4-coverage.json` are ignored generated reports.
Their names avoid Coverage.py's `.coverage.*` parallel-data namespace. CI uploads
the detailed coverage JSON, per-module gate result and existing coverage XML.

## CLI journey

The journey uses [the golden summary](../tests/fixtures/milestone4/journey.json),
the existing mixed ranking bundle, benchmark observations and company payloads.
Three eligible jobs have different compensation, with one conflicting-evidence
review and one excluded job. Names, amounts, company identities and responses are
synthetic. The harness writes only temporary data under a Unicode path with
spaces and punctuation, and invokes `python -m applicant` for each operation.

| Area | Assertions |
|---|---|
| Feedback | Exact run/job membership, retry-safe event IDs, strict batch import/dry run, complete replacement revisions and append-only history. |
| Evaluation | Empty/partial/complete judgments, precision bounds, undefined metrics, separately counted review/excluded judgments, fixed-cutoff reproduction and atomic JSON export. |
| Personalization | Insufficient support, three eligible known-score contributors, unchanged preview eligibility, stale feedback rejection, explicit bounded transfer, idempotent acceptance and current profile revision. |
| Historical comparison | Changed profiles with unchanged posting versions, one explicit enriched version, and no automatic transfer of judgments to another run. |
| Company evidence | Exact associations, strict snapshot import/dry run, explicit enrichment export, dated category scoring, and financial facts retained as separate source context. |
| Benchmarks | Immutable imports, explicit cohort selection, monthly annualization, incompatible definitions, suppression and unavailable cross-currency conversion from the run's frozen FX. |
| Replay and reads | Original report stability, matching original/enriched replays, exactly three completed runs, unchanged database bytes after read/export/rejected operations, database export guards and duplicate-key rejection. |
| Files and isolation | Strict JSON outputs, Unicode paths, private POSIX export modes, cleanup, no database creation during missing-history reads and no inherited host configuration/API credentials. |

The subprocess fixture sets the application clock to **2026-11-04 UTC**, so rating
age and FX freshness do not drift with CI dates. A temporary `sitecustomize.py`
blocks Python socket networking; a probe confirms the guard loaded before any
application command runs. Normal parser, service, transaction and export code
execute in each process. Source acquisition is separately exercised by the
existing fixture-backed Eurostat, BLS, MoSPI and company-client tests.

## Coverage policy

The existing overall **90%** branch-aware floor remains in `pyproject.toml`.
`tools/check_milestone_coverage.py` additionally requires **at least 90%** combined
statement/branch coverage in **each of the 28 new M4 runtime modules**. The script
computes the fraction from integer covered/total counts; a rounded display of 90%
cannot pass a value below the floor. Missing files, missing branch data, invalid
counts and duplicate normalized paths fail. Windows and POSIX paths resolve to
the same module names.

Unapproved excluded lines fail. Coverage.py automatically excludes the type-only
`BenchmarkProvider` Protocol declaration in `domain/benchmarks.py`, including its
ellipsis methods and trailing whitespace/EOF sentinel. That is the sole approved
exception: an AST check verifies the named Protocol contains only declarations
with ellipsis bodies and no decorators/default expressions. The exception never
extends over a following executable statement. No source pragmas or coverage
configuration exclusions were added.

The coverage audit found `services/personalization.py` at **89.86%**, which the
rounded table displayed as 90%. Added regressions cover a profile revision race,
a corrupted acceptance link, legacy-run rejection, unknown dimension importance,
invalid feedback cutoffs and evaluation without export. These exercise the
previously uncovered policies rather than changing the coverage floor.

## Required CI execution

Every existing unit matrix leg runs the installed M4 journey: Linux on Python
3.10–3.13 and Windows on Python 3.12. The step has no conditional skip or allowed
failure. A failure makes the unit job fail and reaches the existing required
`status` gate. The coverage job requires both the overall floor and the new
per-module gate, and retains its JSON artifacts for diagnosis. Workflow tests
check these requirements and ensure all referenced scripts are tracked by git.

The existing saved-page real-browser tier remains a separate Linux job. The
installed M4 journey is fully offline and does not need a browser installation,
API credentials or live provider access.

## Local result and remaining acceptance

Windows validation on **2026-11-04** passed **1,239 tests**, with **13 environment
skips** and **1,433 subtests**. Skips comprise nine missing-browser cases and four
POSIX-only checks. All **28 M4 runtime modules have 100% measured statement and
branch coverage**: **1,901 statements and 648 branches**. Overall coverage remains
**99%** (rounded). The Protocol declaration exception above remains explicit.

The full new journey passed with a freshly rebuilt isolated non-editable package.
The final Protocol-aware coverage gate and workflow checks passed **21 focused
tests and 40 subtests** after the full suite. Ruff, formatting, Windows/Linux
Pyright, lock validation and sdist/wheel builds passed locally.

Hosted Linux/Windows CI has not been run in this session. Live-provider statuses
remain as recorded in [the delivery plan](milestone-4-plan.md): Eurostat passed;
BLS acquisition is partial because official metadata downloads returned HTTP 403;
MoSPI's default TLS client remains incompatible with the upstream legacy
renegotiation requirement. Public AmbitionBox decoding passed; other live company
capture checks remain unverified. Fixture and installed-package success do not
establish full live-provider compatibility. Milestone 4 remains open for the final
delivery/provider audit and unresolved hosted/live acceptance.
