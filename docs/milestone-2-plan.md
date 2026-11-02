# Milestone 2: eligibility and ranking engine

Status: Milestone 2 library/service implementation delivered; hosted validation remains pending.
Reviewed 2026-11-02 against `24b2388` on `codex/ranking-foundation`.
This document refines section 17 of [the original proposal](../Implementation_plan.md)
and follows the [Milestone 1 repository amendments](implementation-plan-review.md).
The policies below are product decisions, not empirically calibrated predictions.

## Deliverable and boundaries

Deliver a library/service pipeline that freezes inputs, normalizes base pay,
evaluates eligibility, computes six explainable dimension results and evidence
confidence, orders opportunities deterministically, and persists a complete run.
Loading and replaying that run must work offline after current inputs change.
Every selected job gets a result, including excluded and unscored jobs.

Survey interaction, `applicant rank`, listing-selection flags, user feedback,
learned weights, and ranking-quality benchmarks remain Milestones 3–4. M2 exposes
the services those commands will call. Existing search, PPP filtering, reviews,
financials, applications, and file exports retain their behavior.

## Repository findings that change the draft

| Existing implementation | M2 consequence |
| --- | --- |
| `domain/ranking.py` provides immutable inputs and supplied results, not an engine | Add pure evaluation functions and service orchestration. Reuse the history repositories. |
| `JobRanking.dimension_scores` permits either an empty map or six numeric scores | Introduce a new result schema with nullable dimension values. Missing culture evidence must not become zero or a fabricated neutral score. |
| `ScoringConfiguration` lacks scoring curves, evidence-selection rules, and freshness policy | Add a versioned engine configuration. Include every behavior-affecting policy in its hash. |
| `CandidateProfile` has preferred regions/modes but no actual work location or mandatory modes | Preserve preferences as soft signals; add explicit hard-constraint fields in a new profile schema. Never infer residence from currency or a preferred region. |
| `JobVersion.evidence` is arbitrary JSON and is already included in both hashes | Put validated, versioned facts inside evidence payloads; no new job identity policy is necessary. |
| `domain/salary.py` uses floats, assumes annual when period is absent, defaults bare `$` to USD, and collapses a singleton into two bounds | Keep its search API. Add a conservative Decimal facts parser for ranking; share lexical helpers only where semantics really agree. |
| `money.Rates` returns float tables and uses `/v1/latest`; its cache does not preserve provider response bytes | Add a ranking FX adapter using the existing `HttpClient`. Do not claim old cached floats retain original precision or silently reuse PPP. |
| Rating adapters already expose culture, career-growth/opportunities, and sometimes job-security categories | Snapshot those categories and score them as disclosed proxies. Overall rating is not a substitute for every dimension. |
| Financial records contain funding, valuation, revenue and headcount, but no cash runway or profitability | Funding or a public-company label alone cannot establish stability. Initial stability scoring uses captured job-security ratings. |
| Schema 3 stores versioned JSON and nullable score/confidence columns | New payload schemas can reuse its tables and integrity triggers. Do not rewrite migration 3 or historical rows. |
| `RankingRunRepository.get()` returns results in input order | Preserve that order; derive ranked views separately using Decimal payload values, not SQL REAL order. |

The focused existing contracts, store, history, salary and domain-boundary tests
passed during this review: **86 passed, 225 subtests passed** using
`uv run --frozen pytest`. The prior complete Windows validation is recorded in the
M1 review; a new full-suite run is an implementation gate, not a claim of this review.

## External references and decisions

The following primary references were checked on 2026-11-02:

| Reference | Finding and resulting decision |
| --- | --- |
| [Frankfurter v1 documentation](https://frankfurter.dev/v1/) | v1 is deprecated but remains available. Leave existing search integration alone; use v2 for new ranking acquisition. |
| [Frankfurter v2 documentation](https://frankfurter.dev/) and [OpenAPI](https://api.frankfurter.dev/v2/openapi.json) | Default rates blend providers. Request the ECB provider explicitly with EUR as its native base. Responses are dated currency-pair records; validate every row and retain the response and request metadata. Do not collapse mixed observation dates into one misleading effective date. |
| [ECB reference-rate documentation](https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html) | Reference rates update on working days and some currencies can be unavailable. Track observation date separately from retrieval time; ISO validity does not imply rate coverage. |
| [Python 3.10 Decimal documentation](https://docs.python.org/3.10/library/decimal.html) | Decimal contexts and rounding are explicit controls. Use a local, fully specified arithmetic context and final quantization; ambient caller settings must not change results. |
| [BLS OEWS methodology](https://www.bls.gov/opub/hom/oews/pdf/oews.pdf) | 2,080 hours is a full-time annualization convention. Keep it as a disclosed configurable assumption, not a claim about the actual contract or take-home income. |
| [Google JobPosting documentation](https://developers.google.com/search/docs/appearance/structured-data/job-posting) | Remote mode and permitted applicant locations are distinct fields. Base salary has explicit units; full-time and contractor categories can coexist. Preserve these distinctions instead of treating remote as worldwide or full-time as permanent. |
| [iso3166 package documentation](https://pypi.org/project/iso3166/) | Provides country-code definitions, including alpha-2 lookup. Pin `iso3166==3.0.0` through uv and record that registry version in new geographic contracts, following the existing currency-registry approach. |

These sources support data interpretation and implementation mechanics. They do
not validate the proposed utility curve, category weights, or confidence formula.

## 1. Versioned contracts and compatibility

Keep schema-v1 readers, validators, canonical hashes, and historical payloads
unchanged. Add named v2 models and explicit discriminated decoding by
`schema_version` for profiles, scoring configurations, FX snapshots, results, and
runs. Unsupported versions fail with `StoreError`; never decode unknown versions
as the latest model. Shared validation should live in a version-neutral base;
avoid changing the existing `Contract.schema_version` default globally.

- `CandidateProfileV2` adds optional `work_country` (validated ISO alpha-2),
  `country_registry`, and `required_work_modes` (empty means unrestricted).
  Existing preferred modes remain soft. An explicit upgrade creates a new profile
  revision, copies old fields, and leaves new constraints unknown/unrestricted.
  Running from a v1 profile uses those same absent-field semantics without
  rewriting the profile or changing its revision.
- `RankingFacts` is an independently versioned payload inside `Evidence` with
  compensation facts, possible work modes, remote scope (`worldwide`,
  `country_list`, `unknown`), allowed countries, residual restriction text,
  employment arrangements, required technologies, and selected company ratings.
  A known-empty set differs from a missing/incomplete extraction. Each fact has
  source identity, observation time, extraction policy, quality category, and a
  reference to its captured source value/text. Contradictory facts stay explicit.
- `CompensationFacts` records optional bounds, whether the amount is exact or
  open-ended, currency, period, gross/net basis, and pay component
  (`base`, `total`, `unknown`). Preserve raw text and matched spans. Do not reuse
  `NormalizedCompensation` as the source-of-truth facts model.
- `ScoringConfigurationV2` includes the policies below, arithmetic context,
  annualization factors, score and confidence precision, technology aliases,
  source/category mappings, source priority, freshness windows, FX selection
  policy, and all numeric coefficients. Use named policy IDs with validated
  supported values rather than arbitrary executable expressions.
- `FXSnapshotV2` retains v1 rates and chronology and adds immutable acquisition
  metadata: request/provider, raw response when fetched, response digest,
  selected observation date, and optional parent snapshot ID for cached copies.
  Snapshot resolution diagnostics belong in run metadata. Explicit offline v1
  snapshots remain usable with their existing, more limited provenance.
- `DimensionResult` contains dimension, status (`known`, `unknown`,
  `not_applicable`, `disabled`, `not_evaluated`), nullable score, quality, reason codes, selected
  evidence references, raw calculation terms, and weighted contribution.
  All non-known scores are null; known zero is valid. `not_evaluated` is reserved
  for excluded jobs whose scoring was skipped.
- `EligibilityDecision` contains one result per criterion (`pass`, `fail`,
  `unknown`, `not_applicable`), stable reason codes, and supporting evidence.
  The aggregate remains eligible/review_required/excluded.
- `JobRankingV2` contains all six dimension results, eligibility details,
  normalization, nullable overall score, confidence, and coverage. Reasons should
  be machine-readable with separate display text. Excluded jobs retain facts and
  decisions but have no fit/confidence calculation.
- `RankingRunV2` identifies the engine/configuration and preserves resolution
  diagnostics. Require exactly one result per input, unlike legacy supplied runs
  which legitimately allowed a subset. Keep exact profile/FX/config IDs, build
  identity, lockfile digest, calculated time and ordered membership.

The engine accepts only v2 scoring configuration; M1 configs describe no complete
algorithm. An explicit factory copies the existing normalization settings into a
new v2 configuration. Never apply current defaults to an old run during replay.
V1 history remains readable; it is not retrospectively claimed to be engine-generated.

No new SQL columns are required for this design. Update repository codecs and
types, not DDL. If implementation later needs indexed metadata, append migration
4 with upgrade/rollback tests; never alter migration 3 or bypass immutability.

## 2. Evidence acquisition and selection

The service captures facts before scoring. Pure domain code receives frozen input
objects and an explicit evaluation time; it performs no HTTP, SQL, file reads,
clock reads, or UUID generation. Revalidate/copy nested containers at the boundary.

For M2, support existing `Job` fields, explicitly supplied structured posting
facts, supplied retained descriptions, and captured `CompanyRating` records.
No new browser requests or broad NLP/LLM extraction are required. Add extraction
only for well-defined formats with regression fixtures; unsupported text stays
unknown. A legacy `remote=True` establishes a remote-mode hint, not geographic
permission; `False` cannot distinguish hybrid from onsite.

Preserve complete selected review payloads with provider company ID/URL and
observation time. Company association must be supplied or resolved by a verified
identifier mapping, not a fuzzy name match. For each category choose the latest
usable observation per provider, then the configured provider priority
(`glassdoor`, `ambitionbox` initially), then evidence hash. Do not average sources
with unknown population overlap. Capture all considered evidence and the selection
reason so later source-priority changes can create a different run.

Conflicting high-quality posting facts produce unknown/review-required decisions;
source order must not silently resolve a hard-constraint conflict. Unrecognized
evidence schemas are preserved but not consumed. Every consumed payload, alias
map and policy participates in a persisted input/configuration hash.

Conflict explanations are part of the M2 result contract, ready for the M3 display
layer. Record `conflicting_evidence`, the affected criterion and route, competing
normalized values, evidence references, source labels, observation times, and
short supporting excerpts. Supply a stable human-readable explanation, for example:
"Remote location needs review: the structured posting permits US applicants only,
but the description says worldwide." Identify the unresolved fact the candidate
needs to verify. Distinguish contradictory assertions about the same route from
valid alternative routes, and distinguish conflicts from simply missing evidence.
Acceptance fixtures must verify both structured details and rendered explanations,
including after persistence/replay; a generic "review required" label is insufficient.

## 3. Compensation normalization

Use only comparable annual base pay. Parse explicit structured values first,
otherwise use the new conservative text parser. Explicit currency codes and
unambiguous symbols are accepted; bare `$`/`¥`, absent periods, absent gross/net
basis, unspecified base versus total pay, bonus-only figures and conflicting
units produce review-required facts. No hidden annual/gross/USD defaults.
Board-specific assumptions can be added only as named, captured extraction rules.

Preserve `from 80k`, `up to 100k`, exact `90k`, and `80k–100k` distinctly. Reversed
ranges are invalid facts, not silently reordered. Known zero is not missing.
Annualization factors: annual 1, monthly 12, weekly 52, daily configured days
(default 260), hourly configured hours (default 2080). Record applied factors;
contract and employee pay are comparable base amounts, not equivalent benefits.

For snapshot rates expressed as quote units per base unit:

```text
annual_target = original_amount * annualization_factor * rate[target] / rate[source]
```

Same-currency conversion uses exactly 1 without requiring a non-base quote. Use
Decimal inputs parsed directly from numeric strings/JSON tokens. Local context:
precision 34, ROUND_HALF_EVEN, Emin=-6143, Emax=6144, clamp=0, capitals=1;
enable InvalidOperation, DivisionByZero, Overflow and FloatOperation traps and
disable the remaining traps. Reject values outside the supported range with an
explained normalization failure. Set all context options, not just precision,
so caller traps cannot change the result.
Quantize annual endpoints and representative pay to 0.01 only at the final
normalization boundary. This is a comparison precision policy, not ISO cash
settlement precision. Eligibility compares these persisted values to annual
profile thresholds quantized using the same policy.

Representative strategy: minimum needs a lower bound, maximum needs an upper
bound, midpoint needs both. Missing required bounds leave representative pay null
while independently usable bounds remain available for eligibility. Never turn
an upper-only offer into a guaranteed salary. Gross/net mismatch, missing rates,
and unknown units return explicit unavailable/review-required outcomes and null
comparable values. No tax or PPP conversion is introduced.

## 4. New-run FX resolution

Historical replay loads its exact snapshot and never invokes this resolver.
For a new run, resolution mode and evaluation time are explicit service arguments:

1. An explicitly supplied snapshot is used exactly after validation; missing
   currencies are reported, never filled from another date/provider.
2. Otherwise, if online and conversion is needed, fetch once per batch through
   `HttpClient`: `/v2/providers/ECB/rates?base=EUR`. Parse Decimal from response
   bytes and retain request/response metadata. Select the latest observation
   date among non-identity rows and retain only rows with that date; add EUR=1
   by definition. Exclude older rows and disclose omissions.
   Reject wrong bases, duplicate conflicting pairs, nonpositive/nonfinite rates,
   future dates, unsupported registry codes and malformed payloads.
3. On network/provider failure or an unusable response, choose a persisted,
   non-static market snapshot with usable conversion pairs and effective age at
   most 7 calendar days. Sort candidates by effective date descending, original
   retrieval time descending, snapshot ID ascending. Cache recapture must preserve
   original retrieval time; it cannot make stale rates fresh. Add a repository
   query method rather than SQL in services.
4. A static snapshot is used only when explicitly supplied and enabled, with a
   dataset version and effective date within the same configured age limit.
   There is no bundled static market table today; do not manufacture one or
   mistake the bundled PPP table for FX. Fallback quality is disclosed.
5. If nothing is usable, create a target-currency identity-only snapshot and
   record `fx_unavailable` for conversions. Same-currency jobs can still normalize;
   other jobs have unknown compensation and remain in the run. An identity
   snapshot never claims cross-currency coverage.

A valid selected snapshot may be partial: missing currencies affect those jobs
only. Do not merge another snapshot into it. A live response older than 7 days is
unusable even if just retrieved. The 7-day window is configurable product policy
to accommodate non-publication days, not a guarantee that a rate is current.
No network operations occur inside a database write transaction. Provider timeouts
and errors appear in resolution diagnostics, without turning expected acquisition
failure into a failed ranking batch. Corrupt persisted historical inputs do fail.

## 5. Eligibility rules

Aggregate all criteria: any explicit failure means `excluded`; otherwise any
material unknown means `review_required`; otherwise `eligible`. Return every
criterion's reason rather than stopping at the first failure. Missing fit-only
dimensions do not by themselves change eligibility.

| Criterion | Pass / not applicable | Fail | Unknown / review |
| --- | --- | --- | --- |
| Annual base-pay floor `F` | No floor: N/A. Known comparable lower bound >= F: pass. | Known comparable upper bound < F. | Straddling range; upper-only at/above F; missing/incomparable pay. |
| Employment | `either`: N/A. An explicit acceptable permanent/contract option exists. | Complete, explicit arrangement set contains no accepted option. | Missing/ambiguous arrangement; `FULL_TIME` alone does not establish permanence. |
| Required work mode | Empty requirement: N/A. A verified offered mode intersects required modes. | Complete offered-mode set is disjoint. | Incomplete/contradictory mode evidence. Preferred modes alone never exclude. |
| Remote location | Nonremote: N/A. Explicit worldwide permission without residual restrictions, or the declared work country is allowed. | Explicit exhaustive allowed-country list excludes the declared work country. | Restricted scope with missing work country; unknown scope; residual state/timezone/authorization restrictions. |

Evaluate remote restrictions only on a remote route that the candidate is actually
considering. Where a posting offers alternative modes, pass if at least one route
satisfies every relevant mode/location condition, fail only if every route
explicitly fails, otherwise review. A viable onsite route must not be excluded by
restrictions applying solely to its remote alternative. M2 does not infer onsite
commutability, relocation, immigration permission, or tax eligibility.

Keep route evaluation linear in the supplied facts, country entries and routes:
normalize/deduplicate facts once, build country membership sets once, and evaluate
job-wide criteria once. Represent country restrictions as sets attached to routes;
do not expand a Cartesian product of modes and countries. Classify relevant
evidence conflicts before route short-circuiting so input order cannot hide them.
Within a route, test cheap mandatory constraints first and stop once it explicitly
fails. Stop alternative-route evaluation once a fully verified route passes;
otherwise retain unknown routes until all alternatives are resolved. Record the
decisive route/reason and mark skipped checks as not evaluated, never as passes.
Route-check trace entries support `not_evaluated`; the job-wide criterion statuses
remain unchanged.
This optimization must preserve the aggregate result and every job-wide criterion
reason promised above; it need not calculate diagnostics for irrelevant routes.
Test decision equivalence against a simple exhaustive reference evaluator on bounded
generated cases, plus predicate-call counts on large synthetic route sets. Avoid
wall-clock thresholds in CI; correctness and bounded work are the acceptance gates.

With lower-only pay below the floor, the offer could still meet it: review. Equality
at an upper-only ceiling still gives no guarantee: review. Hard constraints apply
even when the corresponding scoring dimension has zero weight.

Free-text `preferred_regions` and `timezone_overlap_tolerance` remain visible
preferences with an `unstructured_preference_not_evaluated` explanation when set.
They never silently become country whitelists or legal/working-hour decisions.

## 6. Initial scoring policy

Scores express configured preference utility, not hiring probability. Eligibility
and confidence remain separate. Score eligible and review-required jobs; do not
mix the latter into an unexplained eligible-only list. Excluded results have null
overall score/confidence and uncomputed dimension results.

| Dimension | Deterministic initial rule |
| --- | --- |
| Compensation | For representative pay `x` and minimum `F` / preferred `P`: if `0 < F < P`, use `50*x/F` below F, `50 + 50*(x-F)/(P-F)` from F to P, and 100 above P. Preferred-only (or F=0<P): `100*min(x/P, 1)`. Minimum-only or equal positive thresholds: binary 0 below / 100 at or above threshold. A zero-only threshold provides no useful preference: N/A. No thresholds: N/A. No representative/comparable pay: unknown. Clamp to [0,100]. |
| Remote compatibility | No preferred modes: N/A. Known selectable mode intersects preference: 100; known exhaustive disjoint modes: 0; otherwise unknown. Geographic permission remains an eligibility decision. |
| Technical fit | With nonempty candidate technologies and complete nonempty required-tech evidence: `100 * matched_required / total_required`. Deduplicate exact canonical tokens using a versioned alias map (`py`/`python`, `js`/`javascript` initially); never substring-match `Java` with `JavaScript`. Missing candidate skills or requirements: unknown. Verified no required technologies: N/A. This is skills overlap, not a complete assessment of competence. |
| Company culture | Use captured `company_culture` (AmbitionBox) or `culture_and_values` (Glassdoor). For a verified 1–5 scale, `25*(rating-1)`. No matching category: unknown. |
| Career growth | Same scale conversion, using `career_growth` or `career_opportunities`. No general-rating fallback. |
| Stability | Same scale conversion, using `job_security`. Label this an employee-reported security proxy, not a solvency forecast. Missing category: unknown. |

Experience, discipline and target seniority remain explanatory profile context in
this initial algorithm. Do not manufacture role-level requirements from a title.
Adding a rubric for those fields requires a new configuration/algorithm version
and fixtures. Company-rating proxies remain limited to their observed categories;
funding/valuation fields do not increase stability scores.

For each positive-weight, applicable dimension let `s_i` be its known score,
`w_i` its profile weight, and `q_i` its evidence quality. Let A include applicable
dimensions (including unknown), and K include those with known scores:

```text
overall_score = sum(w_i * s_i for i in K) / sum(w_i for i in K)
coverage      = sum(w_i for i in K) / sum(w_i for i in A)
confidence    = sum(w_i * q_i for i in K) / sum(w_i for i in A)
```

If K is empty: score null, coverage 0, confidence 0. If A is empty: score null,
coverage 0, confidence 0 with `no_applicable_dimensions`. Zero-weight and
not-applicable dimensions are absent from both denominators. An unknown dimension
is still applicable; a parser must not disguise missing evidence as N/A.
Quantize final score to 0.01 and confidence/coverage to 0.0001, half-even.

Keep known-score renormalization explicit: a 100 score with 25% coverage is possible
and is not equivalent evidence to a fully supported 100. Every result/view exposes
coverage and confidence next to the score; do not multiply confidence into fit.
Example with active weights .25/.20/.20: scores 100/0/unknown and qualities 1/.8/0
produce score 55.56, coverage .6923, confidence .6308.

## 7. Evidence quality and stable ordering

Start with existing configurable coefficients: explicit 1, verified .9,
structured .8, inferred .5, fallback .5, unknown 0. These are support indices,
not probabilities. Validate unknown=0. Classify the extraction and source basis,
not merely the presence of a value in a JSON field.

For a dimension requiring several facts, use the minimum quality of its required
facts. Compensation additionally takes the minimum with FX quality (live/cache
.9 within the age window, static .5, same-currency identity 1). A quality-zero
fact cannot establish a known dimension or an exclusion.

Review categories use structured quality multiplied by `min(n/20, 1)` where a
positive review count `n` is available; otherwise use .5 as the count factor.
Category observations older than 180 days are unknown for scoring. The scale,
20-review saturation and 180-day window are versioned policy choices. Reject
future observations; compute age from the run's evaluation time, never wall time
during replay. Do not decay captured posting facts automatically in M2; expose
their capture time. No confidence claim implies factual verification of reviews.

Return separate eligible, review-required and excluded views. Within each scored
view, order non-null scores descending and then `job_id` ascending; null scores
come last, ordered by `job_id`. Excluded view sorts by `job_id`. Compare persisted
quantized Decimal scores, so displayed ties use the documented tie rule. Confidence
is not an implicit tie breaker. Input permutation must not change ranked order.

## 8. Services and historical replay

Suggested ownership, creating modules only when the corresponding code lands:

```text
domain/ranking_facts.py       typed facts and evidence validation
domain/ranking_v2.py          engine config, criteria, dimensions, result/run models
domain/compensation.py        legacy normalization result contract retained
domain/normalization.py       pure Decimal base-pay normalization
domain/ranking_math.py        shared fixed arithmetic context
domain/fx_resolution.py       typed selected snapshot and resolution diagnostics
domain/ranking_salary.py      conservative Decimal facts parser
domain/eligibility.py         salary/employment and evidence conflict predicates
domain/work_routes.py         conflict classification and efficient joint route predicates
domain/eligibility_engine.py  complete decision and supplementary route-check trace
domain/dimension_scoring.py   six dimension utilities and evidence selection
domain/scoring.py             weighted fit/confidence aggregation and job results
domain/ranking_views.py       deterministic views partitioned by eligibility
infra/fx.py                  Frankfurter v2 adapter via HttpClient
infra/store/                 existing repositories plus version-aware codecs/query
services/ranking.py           prepare, resolve, evaluate and persist
services/ranking_fx.py        new-run FX selection/persistence outside write locks
services/ranking_evidence.py  verified-ID rating capture, typed and complete raw evidence
services/ranking_history.py   existing exact loading plus replay verification
domain/ranking_engine.py      pure ordered evaluation of exact immutable inputs
services/ranking_replay.py    read-only replay with artifact identity and result checks
```

`evaluate_jobs(profile, jobs, fx, config, evaluation_time)` is pure and returns
one result per selected job in input order. `rank_jobs(store, ..., build_identity,
lockfile_digest, evaluation_time)` validates inputs, resolves FX outside the write
lock, stores/reuses input records, uses the IDs returned by deduplication, evaluates,
and atomically completes the run through existing repositories. The service
creates the run ID; the pure evaluator does not. Exactly one version of a job may
be selected; reject duplicates with a clear error before writing a run.

Inputs may remain after a failed calculation, but no incomplete run becomes visible.
No current profile/head or listing read is permitted once exact inputs are chosen.
The caller supplies the Store and job selection, preserving current path ownership.

`replay_run(store, run_id, build_identity=..., lockfile_digest=...)` loads the exact bundle, dispatches only to a supported
algorithm implementation, evaluates using the original timestamp, and compares
canonical results including explanations. It never writes a new run or refreshes
inputs. Missing old code produces `unsupported_algorithm`, not execution under
the latest engine. Build identity and lockfile digest identify the required release;
retain an artifact/source tag for reproducibility. A new configuration or newly
captured evidence is a new run, even when the listing text is unchanged.

## Semantic commit sequence

Each implementation commit includes focused tests. These subjects are planned;
none implies M2 functionality already exists.

| Order | Subject | Scope and acceptance |
| --- | --- | --- |
| 1 | `docs(plan): specify milestone 2 ranking policies and delivery sequence` | This researched plan and links from earlier documents. |
| 2 | `feat(ranking): add versioned engine facts and result contracts` | V2 profile/config/FX/result/run models, country registry via `uv add iso3166==3.0.0`, explicit profile/config upgrades, typed evidence and conflict explanations, policy validation, reusable golden fixture loaders, golden hashes and v1 compatibility fixtures. Commit manifest and lock together. |
| 3 | `feat(store): support versioned ranking payloads and fx selection` | Discriminated repository decoding, versioned history bundles, latest eligible cache query, unchanged v1 round trips and SQL guards. Verify schema 3 handles v2 payloads without rewriting rows. |
| 4 | `feat(compensation): normalize explicit base pay with decimal policies` | Conservative facts parser, open ranges, annualization, FX cross-rates, gross/net handling, representative strategy and provenance. Add reviewed cross-currency and multi-tier compensation golden cases; regression tests retain legacy search parser behavior. |
| 5 | `feat(fx): resolve ranking snapshots with recorded fallback provenance` | V2 ECB adapter, raw acquisition capture, deterministic cache/static/identity resolution, age/coverage diagnostics, HTTP mock fixtures and no-network replay checks. |
| 6 | `feat(eligibility): evaluate pay employment and remote constraints` | Structured criteria, three-state aggregation, bounded alternative-route evaluation with safe short-circuiting, exhaustive-reference equivalence and work-count tests, exact-floor boundary cases, country restrictions, incomplete and contradictory evidence with readable explanations. |
| 7 | `feat(ranking): score opportunities with evidence coverage and explanations` | Six policies, captured review adapters, known-only aggregation, independent confidence, freshness rules, stable ordering, source selection and formula fixtures. |
| 8 | `feat(ranking): persist engine runs and verify offline replay` | New-run orchestration, deduplicated IDs, complete results, atomic completion, version dispatch and deterministic result comparison. Reopen/change-current-input journeys. |
| 9 | `docs(ranking): document engine policies and validated library workflows` | Working offline library example, explanations/limits, release notes, installed-wheel run/replay exercise and full existing quality gates. Record actual results. |

Keep commits 2–8 individually reviewable and green; split further if necessary.
Breaking API changes are allowed, but use `!` and a `BREAKING CHANGE:` footer only
for an actual compatibility break. Versioned additions need not be labeled breaking.
Do not squeeze an unrelated search FX upgrade or survey CLI into these commits.

## Acceptance matrix and uv workflow

Meaningful fixtures and properties must cover:

- Annual/month/week/day/hour conversions; custom hours/days; open bounds; zero;
  locale grouping; ambiguous currencies; absent basis/component; total compensation;
  mismatched gross/net; half-even ties; caller Decimal precision/traps isolation;
  invalid/extreme numbers and incomplete FX coverage.
- Same-currency operation offline; one request per batch; malformed/mixed-date
  HTTP records; provider failure; fresh/stale cache; future dates; explicitly
  allowed static fallback; identity-only degradation; exact snapshot replay.
- Floor ranges below/above/straddling/equal; permanent versus full-time contractor;
  remote worldwide/restricted/unknown; optional modes and multiple viable routes;
  country mismatches; unknown values never being the sole cause of exclusion.
- Every score endpoint and null case; fit/confidence independence; zero weights
  excluded from both denominators but not hard constraints; all unknown/all N/A;
  known zero; source conflicts; source priority; missing/old review evidence;
  threshold monotonicity and stable order under input permutations.
- V1 payload/hash preservation; v2 persistence and duplicate prevention; partial
  legacy runs versus complete engine runs; nested mutation isolation; failed-run
  rollback; reopened database; mutated current profile/job/rating/FX/config;
  missing historical inputs; unsupported algorithm and no-network replay.
- An installed-wheel offline run/replay, including both pinned registries;
  architecture rules keeping domain pure and SQL in infrastructure.

Use existing unittest/pytest, Hypothesis, httpx MockTransport, and fixture patterns.
Establish reusable golden fixtures under `tests/fixtures/ranking/` in the contracts
commit and extend them with the normalization commit. Store decimal strings, fixed
UTC timestamps/IDs, source text, schema/policy versions, and independently reviewed
expected facts, normalized bounds, reason codes and calculations. Cover FX cross
rates, rounding boundaries, locale formats, hourly/monthly ranges, base plus
bonus/equity, and alternative seniority/location pay tiers. Ambiguous tiers must
remain distinct or require review, never become one artificial salary range.
Reuse fixture loaders/builders across parser, normalization, eligibility and replay
tests; return fresh objects to prevent shared mutable state. Expected values must
not be generated by the implementation under test or silently refreshed wholesale.

Hypothesis supplements these examples with bounded Decimal/route strategies and
invariants. Use fixed evaluation times and mocked HTTP, with no live rates or random
UUID assertions. Keep shrinking enabled and turn discovered regressions into
named examples or golden cases so reproduction does not depend on a local example
database. Configure explicit example budgets in a shared test profile; avoid
timing-sensitive properties or blanket suppression of health checks. Golden data
changes must explain the policy change and its effect on historical replay.

Do not add a numerical/ML framework for six arithmetic rules. No live API response
should determine CI success. M2 is ready only after the implementation gates pass:

```console
uv sync --locked
uv lock --check
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pyright
uv run --frozen pyright --pythonplatform Linux
uv run --frozen coverage run -m pytest tests -q
uv run --frozen coverage report
uv build
```

Retain the 90% coverage floor, Linux Python 3.10–3.13 and Windows Python 3.12
matrix, and saved-page browser CI. Document local Chromium skips separately from
hosted results; do not call hosted CI verified until it has actually run.
The original planning review made no dependency or runtime changes. Implementation
now includes v2 engine contracts, provenance/conflict validation, explicit profile
and configuration upgrades, and reusable compensation fixtures. Country definitions
are pinned through uv. Existing v1 validation remains available. Decimal salary
parsing, normalization, FX acquisition/fallback and eligibility are implemented.
Scoring, confidence and ranked views are implemented. Run orchestration and complete
engine replay are now implemented, as recorded below.

Initial implementation slices:

- `00ae713` adds versioned facts/configuration/profile/FX/result/run contracts,
  explicit upgrades, conflict explanations, country validation, a golden policy
  hash and reusable compensation fixtures with separately represented pay tiers.
- Version-aware repositories now read/write both payload generations through
  explicit schema dispatch, reject unsupported versions, preserve schema-3 rows
  and constraints, require engine configurations for engine runs, and select
  eligible cached FX by effective age, original retrieval time and stable ID.
  Integration tests recover v2 runs offline after reopening and preserve v1 profiles.

Validation of these slices: **1,024 passed, 13 skipped, 835 subtests passed** in
76.01 seconds under coverage, **97% overall coverage**. Repository lint/format,
Windows and Linux Pyright targets, uv lock validation and package build pass.
The skips are nine unavailable-Chromium cases and four POSIX-only cases. Hosted
CI and installed-wheel acceptance were not claimed by that local run. The
compensation slice follows; no eligibility/scoring execution exists yet.

The compensation slice adds conservative Decimal prose extraction, explicit
structured-tier precedence, source spans and diagnostic codes, annualization,
market cross-rates, static-rate opt-in, open-bound representative strategies and
fixed-context half-even rounding. Normalization lives in `domain/normalization.py`
to avoid a circular dependency between the existing compensation result model and
the v2 configuration that embeds it. Shared arithmetic policy lives in
`domain/ranking_math.py`. Source amounts exceeding supported precision are rejected
rather than silently rounded during extraction. Reusable golden fixtures now
include rounding boundaries, lower-only offers and base-plus-bonus text.

Compensation validation: **1,041 passed, 13 skipped, 913 subtests passed** in
76.84 seconds under coverage. The three new arithmetic/parser/normalization modules
have **100% statement and branch coverage** (159 statements, 74 branches); total
project coverage is **98%**. Bounded Hypothesis properties supplement golden and
boundary fixtures. Repository lint/format, Windows and Linux Pyright, lock validation
and package build pass. An isolated offline installation of the built wheel passed
a parse/normalize journey, yielding EUR 52,800 from the illustrative monthly USD
range. Hosted CI, live FX acquisition and engine replay remain unverified here.

The FX slice uses the documented unblended ECB v2 endpoint (OpenAPI rechecked
during implementation) through `HttpClient`. It preserves UTF-8 response text,
SHA-256 digest, Decimal tokens, request URL and provider identity. It rejects
malformed/nonfinite/duplicate-conflicting/future observations and discloses older
rows omitted from the latest observation date. Service resolution persists one
exact snapshot, with explicit offline/supplied/static modes, seven-day default
freshness, cache lineage and identity-only degradation. Caller-provided batch
time is used for acquisition/capture metadata. Explicit supplied snapshots must
meet chronology, freshness and static opt-in policy; they never silently fall back.
No acquisition occurs while a write transaction is active. Existing historical
loads remain exact and do not import or invoke the new resolver.

FX delivery is split into two semantic commits: `a3dd624` captures and validates
ECB responses; the following resolver commit implements selection/persistence and
documents the public library API. Validation: **1,056 passed, 13 skipped, 940
subtests passed** in 74.82 seconds under coverage. The new adapter, resolution
contract and resolver have **100% statement and branch coverage** (162 statements,
48 branches); total coverage remains **98%**. Lint/format, both platform type
checks, uv lock validation and package build pass. An isolated offline wheel
installation passed mocked acquisition and real SQLite cache resolution. Tests use
no live exchange-rate response. Hosted CI and complete engine replay remain
unverified; the next implementation slice is three-state eligibility and efficient
work-route evaluation with readable conflict reasons, now delivered below.

Eligibility delivery starts with `fc708a1` for salary/employment predicates and
supported-evidence conflict explanations, followed by joint work-route evaluation.
The initial engine trusts explicit/verified/structured facts only when their
configured support is positive; inferred/fallback evidence remains unknown for
hard constraints. Distinct salary tiers require explicit selection, and normalized
pay must match source facts. Missing bounds, incompatible units, incomplete options
and unstructured regional/timezone preferences do not silently exclude jobs.

Mode/location constraints are represented as one joint `work_route` criterion;
individual checks appear in a supplemental trace. This refinement prevents mode
and location passes from different routes combining incorrectly. Repeated route
IDs across evidence bundles identify assertions about the same route; distinct
IDs identify alternatives. All assertions are classified once before predicates
short-circuit. Skipped routes retain their classified conflicts in the trace.
Decision status is invariant under alternative ordering; the diagnostic trace
records the actual traversal and skipped work. No Cartesian product is constructed.
Two stable conflict witnesses suffice, selected in linear work without enumerating
all pairs. Predicate-count tests cover large alternative sets, and bounded
Hypothesis cases compare decisions with an independent exhaustive reference.

The pure `evaluate_eligibility` API returns `EligibilityEvaluation`. Its decision
fits existing `JobRankingV2` payloads, preserving historical v1/v2 schemas; extra
route-check traces and preference notes are returned separately. A reusable golden
fixture verifies that US-only/worldwide conflict explanations, source excerpts and
verification prompts survive a completed run and offline database reopening.

Eligibility validation: **1,072 passed, 13 skipped, 985 subtests passed** in
67.14 seconds under coverage. The three new eligibility modules have **100%
statement and branch coverage** (219 statements, 94 branches); total project
coverage remains **98%**. An additional focused history check recovers the exact
captured evidence and reproduces its decision offline. Lint/format, Windows/Linux
Pyright, lock validation and package build pass. An isolated offline wheel install
verifies both country exclusion with residual unknown restrictions and an eligible
onsite alternative. Unknown restrictions do not erase explicit country failures.
Hosted CI and complete engine replay remain unverified. Weighted scoring,
evidence confidence and deterministic ranked views are delivered below.

Scoring delivery starts with `0589ac2`: six dimension utilities, captured category
rating selection, known-only aggregation, independent confidence/coverage and
completed job result construction. A second commit adds partitioned ranked views
and the coverage audit. Fixed-context Decimal arithmetic is used throughout;
dimension scores are quantized before weighted aggregation so recorded dimension
values reproduce the aggregate. Contributions preserve full policy precision.
Selected review windows are exact elapsed days (including microseconds), unlike
the separately specified calendar-day FX window. Underflowing aggregation weights
fail explicitly instead of silently producing a misleading zero fit.

Company-rating capture preserves the complete legacy payload and typed categories
for caller-verified provider company IDs. Float-derived values are identified as
the legacy stated value, not original provider precision. No live company lookup,
fuzzy association, overall-rating fallback or financial stability inference is
introduced. Unknown/conflicting technical or compensation evidence remains distinct
from zero; hard eligibility continues independently of scoring weights.

Computed-score history tests recover typed and raw evidence and reproduce the
stored fit/confidence after database reopening with network access forbidden.
Additional contract/recovery tests reject contradictory exclusion states, duplicate
job membership, mismatched normalization inputs, non-object metadata and SQL-key
versus result-payload disagreement. Ranking views compare quantized score keys,
preserve stored values, and remain invariant under input permutations.

Scoring validation: **1,095 passed, 13 skipped, 1,041 subtests passed** in
79.25 seconds under coverage. The four new scoring/view/capture modules have
**100% statement and branch coverage** (246 statements, 108 branches); overall
coverage remains **98%**. The [coverage review](ranking-coverage.md) records the
existing v2 contract coverage (99%) and completed-run repository coverage (100%),
including the one defensive contract guard already preceded by Pydantic rejection.
Repository lint/format, Windows/Linux Pyright, lock validation and package build
pass. An isolated offline wheel installation passes the public
parse/normalize/eligibility/score/view pipeline. Hosted CI, run orchestration and
complete engine replay were pending at that validation point and are delivered below.

Run delivery starts with `1cb82e9`, adding run payload schema 3 for per-job tier
choices. Existing v1/v2 readers and validators remain available; shared fields
preserve their original semantics. The new payload uses existing SQLite schema 3
without DDL changes. Choices remain run-specific, leaving posting hashes unchanged.

The pure evaluator reads only supplied snapshots and returns one result per input
in membership order. New-run orchestration validates the exact profile revision,
prepares deterministic captured facts, saves/reuses configuration/job IDs, resolves
one FX snapshot outside write locks, evaluates, then atomically completes the run.
Legacy raw fields receive conservative typed extraction only when that category
is absent. Parser diagnostics and unknown-schema notes remain in the decision.
Future captures/consumed observations and duplicate identities fail explicitly.

Replay supports the frozen-inputs-v1 schema-3 engine policy and requires the
retained artifact's build/lock identity. It performs no writes or fallback reads,
uses original time and choices, compares canonical results including explanations,
and returns explicit mismatched membership IDs. Older supplied-result formats are
still loadable but raise unsupported_algorithm for engine replay. Identity checks
compare caller-supplied metadata; they do not attest a binary or fetch source.
The corresponding release/source artifact must be retained by the caller.

Acceptance journeys cover mixed eligibility states, empty runs, config/job
deduplication, independent tier choices with shared job versions, updated current
profile/rate/config/job state, offline reopening, missing support, mismatch reports,
calculation failures and a SQL-trigger failure during completion. A three-job mock
batch asserts one provider request with no write transaction active. The runnable
`examples/ranking_offline.py` composes the public services in a temporary database.

Final local M2 validation: **1,105 passed, 13 skipped, 1,059 subtests passed** in
76.77 seconds under branch-enabled coverage. Pure batch evaluation, orchestration
and replay have **100% statement and branch coverage** (180 statements, 72 branches);
overall project coverage remains **98%**. Lint/format, Windows/Linux Pyright, uv lock
validation and package build pass. The offline example ran against the built wheel
in an isolated uv environment and returned eligible, score 81.48, confidence .4,
coverage .45 and replay_matches=true. The updated [coverage review](ranking-coverage.md)
records contract and recovery coverage. Hosted Linux/Windows/browser CI execution
remains unverified; commits have not been pushed. The M2 library/service workflow
is implemented locally. Survey interaction and ranking commands remain Milestone 3.
