# Milestone 4 delivery and acceptance audit

Audited locally on Windows on **2026-11-04**, against implementation commit
`51d3677` on `codex/ranking-foundation`. All ten planned local delivery phases
are represented, including this audit. **Milestone acceptance remains open**:
full live BLS and default-client MoSPI acquisition, and hosted CI execution, are
not verified. This document separates delivered code from those acceptance gates.

## Delivered capabilities

| Accepted capability | Delivered behavior and evidence |
|---|---|
| Candidate feedback | Append-only shortlist/dismiss/clear events reference exact completed run/job/version membership. CAS revisions, complete replacement state, required dismissal reasons and retry-safe IDs are enforced. Importance feedback remains independent of dismissal reasons. |
| Comparison and evaluation | Read-only comparisons retain membership/version changes, eligibility groups, positions, scores, dimensions and changed input/provenance records. Evaluation freezes sequence cutoffs and event IDs, reports judgment coverage/precision bounds, and leaves undefined precision/recall null. |
| Explicit personalization | `bounded-transfer-v1` requires three known-score eligible supporters for both dimensions. Frozen-score previews preserve eligibility and disabled dimensions. Acceptance checks feedback/profile freshness and atomically records the new profile revision and audit link. |
| Benchmark foundation | Immutable raw responses, snapshots and cohort observations retain units, periods, classification and methodology. Decimal annualization and explicit frozen FX preserve incompatible definitions, missing rates, suppression and censoring as context. |
| Three benchmark adapters | Eurostat SES JSON-stat, bounded BLS OEWS series requests, and paginated MoSPI PLFS salaried earnings share the provider abstraction and HTTP infrastructure. Complete acquisition precedes persistence. Provider fixture tests exercise each adapter; live status is recorded below. |
| Company evidence | Exact job-version/provider-ID associations, bounded raw capture through all four existing clients, strict imports and explicit snapshot selections produce enriched new job-version bundles. Known-date ratings use existing scoring policies; unknown dates and financial information remain context. |
| CLI and packaging | The full M4 subprocess journey validates feedback, cutoffs, stale/idempotent acceptance, enrichment, benchmarks, comparison, strict exports and old-run replay in temporary Unicode paths with a fixed clock and forbidden networking. |
| CI and coverage | Existing Linux 3.10–3.13 and Windows 3.12 unit legs require installed M4 execution. The coverage job enforces the overall floor and a separate floor for all 28 new M4 runtime modules. Workflow tests prevent conditional/allowed-failure escapes and missing tracked scripts. |

Application outcomes remain independent of candidate judgments. Retrospective
metrics remain descriptive and disclose selection bias. Benchmarks do not affect
eligibility, scoring or salary preferences. Financial amounts never become a
financial-health score. Source disagreement and high-quality posting conflicts
remain visible through the existing evidence and review policies.

## Persistence and compatibility

Migrations append schema **4** (feedback), **5** (suggestions/acceptance), **6**
(benchmarks) and **7** (company evidence). Earlier migrations were retained.
Tests cover upgrades from every supported prior schema, rollback, foreign keys,
immutable update/delete/replace guards, concurrent feedback edits, atomic
acceptance and payload/key/hash recovery. Existing databases can be upgraded with:

```console
uv run applicant db upgrade --database PATH
```

Historical reads remain read-only and explain required upgrades. `ranking-1` and
existing profile/run formats remain unchanged. New artifacts have independent
schema/policy versions. Enrichment adds posting versions; original runs continue
to read and replay exact original inputs. Public APIs and legacy browser clients
are refreshed only by explicit acquisition commands.

No changes to `pyproject.toml` or `uv.lock` were needed across the M4 feature
commits. Existing shared HTTP, file export, SQLite and Decimal infrastructure is
used throughout. No runtime dependency or scoring model was introduced by M4.

## Validation and coverage

The fresh branch-enabled full suite at `51d3677` passed **1,239 tests**, with
**13 environment skips** and **1,433 subtests**, in **196.56 seconds**. Nine skips
require Chromium; four exercise POSIX-only behavior. This is Windows execution,
with Linux also checked through Pyright's platform target.

All **28 new M4 runtime modules have 100% measured statement and branch coverage**,
covering **1,901 statements and 648 branches**. The required unrounded 90% module
gate passed. Overall coverage is **99%** (rounded), above the unchanged 90% floor.
The only approved automatic exclusion is Coverage.py's type-only
`BenchmarkProvider` Protocol declaration and trailing whitespace/EOF sentinel;
the gate checks its AST. No runtime source exclusion pragma was added.

| New runtime area | Modules | Measured coverage |
|---|---:|---:|
| CLI | 5 | 100% |
| Domain contracts, policies and provider decoders | 9 | 100% |
| Provider capture and persistence infrastructure | 8 | 100% |
| Services | 6 | 100% |

Existing modules modified by M4 retain their broader pre-existing branches:
Crunchbase **97%**, Tracxn **97%**, SQLite **97%**, AmbitionBox **96%** and
Glassdoor **99%**. Remaining paths include provider error wrappers, optional
financial/review extraction and paging, browser behavior, and SQLite environment
availability/older document-import branches. CLI dispatch/rank and ranking-report
modules are **100%**. These figures are separate from the 28-module result;
validators and coverage settings were not weakened to obtain 100% globally.

Ruff, formatting, Windows/Linux Pyright and lock validation passed in this audit.
The sdist and wheel built successfully. The M4 journey passed against the actual
`dist/applicant-0.2.0-py3-none-any.whl` with runtime versions exported from
`uv.lock`, without development dependencies. An initial offline attempt found
uncached `greenlet`, `playwright` and `pydantic-core` wheels; downloading those
locked versions allowed the artifact journey to run. The wheel's SHA-256 is
`4bf0e47d64d83dffbc3195cfd70f8b31ac1575f73271d352744f7203fb67e911`.
The subsequent fully offline run of the same wheel and pinned requirements
also passed the full M4 journey.

The [validation guide](milestone-4-validation.md) records the full CLI scenario
and CI gate policy. Subprocess execution is a behavior/packaging check; child
coverage is not merged into the ordinary in-process module measurements.
Fixture provenance is caller-supplied; replay verifies recorded identifiers and
inputs. Binary attestation is outside this check.

## Live-provider recheck

The [machine-readable provider ledger](milestone-4-provider-validation.json)
records exact query documents, CLI exit codes, response digests, snapshot identity
and recovery checks. Fresh attempts used the normal CLI and default verified
HTTPS. Successful raw responses, exports and SQLite data remain in the ignored
local directory named in that ledger. No benchmark database was created for
either unavailable provider.

| Provider | Current result | Verified scope / open gate |
|---|---|---|
| Eurostat | **Verified** | Discovery, selected DE/FR OC1/OC2 acquisition, four observations, private JSON export, SQLite persistence and exact offline recovery passed. Both raw responses are archived. |
| BLS OEWS | **Unavailable for complete acquisition** | The normal fetch returned exit 1, `BLS HTTP 403`, at official metadata access. A separate public wage-API diagnostic returned two 2025 A01 series successfully. That diagnostic does not validate metadata/cohort selection or establish full adapter acquisition. |
| MoSPI PLFS | **Unavailable with default TLS** | The normal fetch returned exit 7 with `UNSAFE_LEGACY_RENEGOTIATION_DISABLED`, before persistence. This audit did not enable legacy renegotiation or disable certificate/hostname verification. Earlier research-only response decoding remains partial evidence. |
| AmbitionBox | **Verified public decoding/capture only** | One public response decoded with provider company ID `42`; exact raw bytes were captured. No job/company association was independently verified or stored, and source observation time remains unknown. |
| Glassdoor, Crunchbase, Tracxn | **Unavailable in this audit** | Live checks were not performed: the configured test Chromium executable is unavailable and authenticated APIs were not exercised. All four company-client capture paths pass offline fixture tests. |

The [BLS series layout](https://download.bls.gov/pub/time.series/oe/oe.txt) and
[official PLFS API specification](https://raw.githubusercontent.com/nso-india/esankhyiki-mcp/main/swagger/swagger_user_plfs.yaml)
were rechecked as primary references. Browser/cache access to documentation does
not establish runtime endpoint availability. There is no cohort/release fallback
or transport-security downgrade to turn an unavailable probe into success.

Acquisition exports are result envelopes. Their `snapshot` member is the strict
`BenchmarkSnapshot` import document; offline `show` can read the stored snapshot
directly. The ledger retains response hashes and public metadata, without copying
the full source bodies into version control.

## Reproduce acceptance checks

Use a new database/output path for each live attempt, so a failure cannot be
mistaken for a snapshot retained from an earlier attempt:

```console
uv run --frozen coverage run -m pytest tests -q
uv run --frozen coverage report
uv run --frozen coverage json -o coverage-m4.json
uv run --frozen python tools/check_milestone_coverage.py coverage-m4.json
uv run --frozen --isolated --no-editable --refresh-package applicant --reinstall-package applicant python tools/smoke_milestone4.py --require-installed
uv lock --check
uv build
uv export --frozen --no-dev --no-emit-project --format requirements-txt --output-file .cache/m4-wheel-requirements.txt
uv run --no-project --isolated --offline --with-requirements .cache/m4-wheel-requirements.txt --with dist/applicant-0.2.0-py3-none-any.whl python tools/smoke_milestone4.py --require-installed
```

Create `.cache` before exporting requirements. The actual-wheel command uses the
exported lockfile runtime pins and requires cached dependencies in offline mode.
It does not use the editable checkout for application imports. Provider checks
can be reproduced independently, using the same public query documents:

```console
uv run --frozen applicant --no-log-file benchmark fetch --query tests/fixtures/benchmarks/eurostat-query.json --database .cache/eurostat-check.db --export .cache/eurostat-check.json
uv run --frozen applicant --no-log-file benchmark fetch --query tests/fixtures/benchmarks/bls-query.json --database .cache/bls-check.db --export .cache/bls-check.json
uv run --frozen applicant --no-log-file benchmark fetch --query tests/fixtures/benchmarks/mospi-query.json --database .cache/mospi-check.db --export .cache/mospi-check.json
```

Fixture and installed-package checks belong in CI; live checks remain separate.
Provider access errors do not authorize changing cohorts, guessing mappings,
using older releases or weakening TLS defaults.

## Remaining acceptance gates

1. Observe the required hosted `status` result for the delivered commit, including
   every Linux/Windows installed journey, coverage/module gates and browser tier.
2. Complete BLS acquisition with accessible official mapping tables, then export,
   persist and recover the selected snapshot through the normal provider path.
3. Complete MoSPI acquisition with the normal verified transport and repeat the
   metadata, pagination, selection, persistence and recovery checks.
4. Run browser/authenticated company checks when available, recording exact
   returned IDs and source observation-date limitations. Fixture success alone
   does not establish live company compatibility.

Local implementation and audit delivery are recorded. The accepted milestone's
all-adapter live-compatibility condition remains unsatisfied, so Milestone 4 is
not marked complete.
