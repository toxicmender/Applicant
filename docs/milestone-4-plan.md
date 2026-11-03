# Milestone 4 — Personalization, Evaluation, and Evidence

Accepted against `6e0dfd7`. Delivery uses semantic commits and uv. All features
preserve exact historical inputs; hosted CI and live providers require separate
verification. Implementation status is recorded as each phase passes validation.

## Policies and sequence

1. `feat(feedback): persist revision-safe candidate judgments`: append-only,
   run/job/version anchored shortlist/dismiss/clear events, explicit importance
   directions, CAS revisions, idempotent imports, frozen sequence cutoffs, and an
   explicit `db upgrade` command. Application logs are independent.
2. `feat(evaluation): compare historical runs and report judged ranking quality`:
   same-profile comparisons, exact membership/version changes, eligible-only
   precision bounds, complete-judgment precision/recall, null undefined metrics,
   frozen event IDs and input digests, offline atomic exports.
3. `feat(personalization): preview and accept bounded weight suggestions`:
   bounded-transfer-v1 requires net support from three eligible known-score jobs
   for both donor and recipient. Transfer min(.05, donor/2, 1-recipient), stable
   dimension-order ties, preserve zero weights/hard constraints, aggregate stored
   dimensions only. Acceptance atomically checks profile and feedback cutoff,
   appends a profile revision, and records an idempotent acceptance link.
4. `feat(benchmarks): add immutable observations and cohort normalization`:
   provider/query/snapshot/observation contracts, raw response retention, Decimal
   annualization and explicit frozen FX, strict imports and offline reports.
5. `feat(benchmarks): integrate Eurostat earnings data`: JSON-stat SES
   earn_ses22_28 annual means and metadata discovery.
6. `feat(benchmarks): integrate BLS occupational wages`: bounded time-series API
   requests and official classification mappings, annual mean/percentiles.
7. `feat(benchmarks): integrate MoSPI salaried earnings`: discover PLFS filters,
   regular salaried monthly earnings with quarterly reference periods and survey
   design distinctions; retain TLS verification.
8. `feat(evidence): capture verified company snapshots and enrich job versions`:
   exact provider-ID associations, existing four providers and strict imports,
   rating capture through existing scoring policies, financial context only,
   enriched new job-version bundles with explicit snapshot selection.
9. `test(milestone): gate personalization and evidence journeys across platforms`.
10. `docs(milestone): record delivery coverage and provider validation`.

Append schema migrations 4 (feedback), 5 (suggestions), 6 (benchmarks), 7
(company evidence); never edit shipped migrations. Reuse existing Store,
transactions, immutable guards, HTTP infrastructure, and argparse boundaries.
Keep ranking-1 and profile/run formats unchanged. M4 contracts have separate
schema and policy versions. Candidate judgments never become employer outcomes.

Benchmark consolidation preserves cohorts: wage definition, population,
geography, occupation system, period and statistic. No blending of incompatible
aggregates, percentile-to-seniority mapping, tax/PPP/inflation inference, or
automatic ranking changes. Job/cohort selection is explicit. Missing feedback
is unknown, dismissal reasons do not imply weight directions, and retrospective
metrics do not establish unbiased recommendation improvement.

## References

- [Selection bias](https://arxiv.org/abs/1602.05352)
- [Offline evaluation methodology](https://arxiv.org/abs/2307.14951)
- [BLS wage definition](https://www.bls.gov/oes/current/oes_tec.htm)
- [BLS series layout](https://download.bls.gov/pub/time.series/oe/oe.txt)
- [BLS API](https://www.bls.gov/developers/api_signature_v2.htm)
- [Eurostat API and revision policy](https://ec.europa.eu/eurostat/en/web/user-guides/data-browser/api-data-access/api-introduction)
- [SES earnings definition](https://ec.europa.eu/eurostat/cache/metadata/en/earn_ses2022_esms.htm)
- [Official PLFS specification](https://raw.githubusercontent.com/nso-india/esankhyiki-mcp/main/swagger/swagger_user_plfs.yaml)

## Acceptance

Golden fixtures cover sparse/corrected feedback, mixed eligibility, changed
versions, conflicts, cross-currency arithmetic, incompatible wage definitions,
suppression and survey changes. Test migrations from every supported version,
rollback, immutable data, FK/key integrity, CAS concurrency and atomic acceptance.
Use recorded/synthetic HTTP fixtures in CI; separately disclose live validation.
Check cutoff reproducibility, null denominators, deterministic ties, suggestion
bounds and zero weights, no route/provider work during previews, provider-level
errors, pagination and sparse JSON-stat, exact company IDs, historical replay,
Unicode paths and installed-wheel journeys. Run locked uv tests and branch
coverage, Ruff, Windows/Linux Pyright, lock validation, build and installed smoke.
Retain the overall 90% floor and require 90% for each new runtime module, reviewing
uncovered branches explicitly.

## Delivery status

Delivered locally:

- Feedback and explicit database upgrade (`8024b32`, schema 4).
- Historical comparison and descriptive evaluation (`be114d0`).
- Bounded suggestions, grouped previews and atomic profile adoption (schema 5).

The latest full branch-enabled run passed 1,173 tests with 13 environment skips
and 1,206 subtests. Overall coverage reports 98%; the 11 new runtime modules have
98% combined statement/branch coverage (523 statements, 122 branches), with each
at least 90%. Remaining gaps are the no-export CLI branch, feedback cutoff
rejection through the report service, irrelevant importance feedback, a legacy
suggestion rejection, generation-time profile race, and corrupted acceptance
metadata guards. Existing validation tests cover the underlying revision and
integrity policies; no coverage exclusions were added.

Still pending: benchmark contracts/storage/normalization and all three provider
adapters; verified company associations/snapshots/enrichment; M4 installed CLI
journey and hosted/live validation; final complete-milestone coverage audit.
This is a partial delivery and does not mark Milestone 4 complete.

Ruff, formatting, both platform Pyright targets, uv lock validation, build and
the existing isolated installed-package ranking journey passed locally.
