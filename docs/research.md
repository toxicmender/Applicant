# Job research workflows

Research is independent of ranking and application submission. New sources are
explicitly selected; the four existing default search boards remain unchanged.
Upgrade an existing database with `applicant db upgrade --database applicant.db`.
Research uses schema 8 and never changes archived ranking payloads.

## Install and inspect sources

```powershell
uv sync --locked --extra pdf
uv run applicant sources list --json
uv run applicant sources show otta
uv run applicant sources health --provider wellfound
uv run applicant sources probe --provider wellfound
```

`otta` resolves to `welcometothejungle`. The catalog distinguishes existing
adapters, public RSS, structured-public-page adapters, and import/reference
sources. Fixture validation does not establish current live access. A probe is
explicit network work; it observes cooldowns and may retry a degraded parser once.
Do not confuse successful authentication with provider permission to automate an
operation. Consult the provider's access requirements before using live capture.

| Domain | Sources |
|---|---|
| Existing discovery | LinkedIn, Indeed, Naukri, Google Jobs |
| Public RSS | We Work Remotely, with original posting links and attribution |
| Public structured jobs, otherwise import | Wellfound, Built In, Welcome to the Jungle, Arc, Instahyre |
| Salary import/reference | Levels.fyi, NodeFlair, Payscale, Comprehensive |
| Culture | Existing Glassdoor/AmbitionBox snapshots; Blind import/reference |
| Company context | Existing Crunchbase/Tracxn snapshots; explicitly associated public profiles |
| Flexible workplace profiles | Flexa, Wellfound, Built In, Welcome to the Jungle |
| Additional import/reference | Dice, ZipRecruiter, Turing, Upwork, Recrew |

## Authentication

To troubleshoot provider setup, send browser diagnostics to a dedicated run log.

```powershell
uv run applicant -v --log-file logs/tracxn-setup.log setup provider tracxn
```

The log shows browser channel attempts, login and verification stages, recognized
network errors, main-document HTTP failures, and cleanup. The standard run log
captures these details too. Use `-v` to also display them in the terminal.
Diagnostics include URL origins and exclude credentials and session contents.
If the worker exits abruptly, the log marks its diagnostics as incomplete.

```powershell
uv run applicant setup provider glassdoor
uv run applicant setup provider linkedin --reauth
uv run applicant setup status --provider linkedin
uv run applicant setup status --provider linkedin --check
uv run applicant setup import-session --provider linkedin --from-file state.json --verify
uv run applicant setup import-session --provider linkedin --from-file cookies.json --format cookies --dry-run
```

Setup performs authentication only. Complete login/MFA in the visible browser.
Login and status verification use provider-specific authenticated-page markers;
unknown or changed markers fail closed. Noninteractive setup returns `needs_setup`
and the import command. It never starts a visible browser on a server.

Default imports accept Playwright `cookies` and `origins`/`localStorage`. Cookie
arrays require `--format cookies` and always produce a storage-completeness warning,
including during dry-run. IndexedDB, sessionStorage, arbitrary tokens and other
unsupported sections are rejected. Playwright storage state does not include
sessionStorage; full-state import is therefore not a universal login guarantee.

An import without `--verify` is unverified. Use `--replace` to replace an existing
session. Failed verification preserves it. Session files and status metadata are
private and never enter research evidence. Per-profile OS locks prevent concurrent
mutation. Imported profile state is consumed by existing browser-backed providers.
Existing legacy login interfaces remain available.

## Templates and company identity

```powershell
uv run applicant research template --kind compensation --output evidence.json --export-schema research-schema.json
uv run applicant research import --from-file evidence.json --dry-run
uv run applicant research import --from-file evidence.json
```

Template kinds are `compensation`, `culture`, `policy`, `technology`, `job-party`
and `corporate-event`. Templates contain synthetic placeholders to replace with
actual source material. Imports are assertions, not independent authentication
of an upstream page. Unknown dates should remain null.

The envelope contains `companies`, `names`, `identifiers`, `events`,
`observations`, `parties`, and `evidence`; unused arrays can be omitted. IDs are
retry-safe: identical content is accepted, conflicting reuse fails atomically.
Foreign keys reject orphan observations, company links, and evidence.

A rename keeps the same entity. Acquisition links the target (`subject_id`) to
the buyer (`related_id`); it does not merge their records. Merger and spin-off
events also keep explicit identities. Event corrections use a new ID and
`supersedes`. An undisclosed recruitment client stays null. Record advertiser,
agency, hiring employer, and employer of record as separate posting parties.

```powershell
uv run applicant research backfill-companies
uv run applicant research bridge-company --snapshot EXISTING_COMPANY_SNAPSHOT --entity-id VERIFIED_ENTITY
uv run applicant research capture-profile --from-file profile-selection.json
```

Backfill uses only already verified schema-7 provider associations and preserves
provider-specific identities; it never merges companies by name. Bridging an old
snapshot requires its provider/company ID to match a verified identifier on the
selected entity. Raw provenance and unknown source dates are preserved.
Profile selections require `entity_id`, `provider`, `provider_company_id`, and
`url`, matching an explicit verified identifier. Only a uniquely identified
structured organization description is accepted; missing data requires import.

## Feed collection and matching

```json
{"providers":["weworkremotely"],"keywords":"Python","limit":25}
```

```powershell
uv run applicant feed collect --query query.json --output feed.json
uv run applicant feed collect --from-imports --output imported-feed.json
uv run applicant feed show --feed SNAPSHOT --format markdown --limit 20 --offset 0
uv run applicant feed refresh --feed SNAPSHOT
```

For public structured providers, `urls` optionally maps provider IDs to at most
25 explicit public job URLs. This avoids relying on changing discovery pages.
Every URL must belong to its selected provider. No page without structured
JobPosting data becomes a successful empty parse.

Requisition URLs or verified employer identity plus requisition ID establish
identity. Company/title/location similarity is only a possible match. Agency
requisition IDs are never substituted for employer IDs. Board count and syndication
lineage are disclosed separately; two syndicated copies are not independent
corroboration. Generic source absence or HTTP errors never imply closure.

Failures preserve successful provider observations. `--require-all-sources` makes
partial results nonzero. Auth failures return `needs_setup`; CAPTCHA/403 triggers
24-hour cooldown; 429 uses the longer of one hour and Retry-After after bounded
attempts. Three consecutive network failures cool down for 15 minutes. Parser
failure marks the adapter degraded. A leased explicit probe tests recovery.

## Analytics and reports

```powershell
uv run applicant analytics technologies --as-of 2026-11-10 --timezone Asia/Kolkata --group-by technology
uv run applicant analytics technologies --as-of 2026-11-10 --group-by stack --origin direct
uv run applicant analytics show --snapshot ANALYTICS_SNAPSHOT
```

Analytics stores the exact participating job_version_ids, fact IDs, match revision,
knowledge cutoff, exclusive window end, and FX snapshot. Rendering never recalculates
membership. Later imports, closures, grouping changes and FX updates cannot mutate
old results. Default demand counts distinct known-open jobs observed in the prior
30 days. Counts are observed openings, not employer headcount or a market census.

Required, preferred, mentioned, negated, and alternative-group technology facts
remain distinct. Salary comparisons require compatible annual gross base pay;
monthly/weekly periods use explicit annualization factors. A range uses its labeled
midpoint. Five distinct comparable jobs are required for a salary leaderboard.
`--currency` and `--fx-snapshot FILE` explicitly select conversion. Different
currencies stay separate when no conversion is requested. Missing FX excludes
incompatible salary values rather than guessing. Technology/pay associations
are descriptive, not causal.

Company report selection example:

```json
{"entity_ids":["VERIFIED_ENTITY"],"as_of":"2026-11-10","timezone":"Asia/Kolkata"}
```

```powershell
uv run applicant research report --selection selection.json --summary --format markdown --output report.md --pdf report.pdf
uv run applicant research report --selection selection.json --section compensation,policy,inherited-context
uv run applicant report render --from-file report.md --pdf rendered.pdf
```

Selection can name exact evidence IDs or job_version_ids. Sections are jobs,
compensation, culture, policy, company, technologies, inherited-context. Summaries
show at most five rows per section with full row counts. Inherited Context remains
visible by default and labels pre-acquisition evidence with original entity and
date context; old policies do not become a successor's current policy.

Default stale thresholds: jobs 14 days, policy 90 days, culture/company 365 days,
compensation 18 calendar months. Override through the selection's `freshness`
mapping (`jobs`, `policy`, `culture`, `compensation_months`). Unknown source dates
remain freshness_unknown regardless of acquisition time.

Schedules must identify the employer timezone explicitly. US Eastern regional time
uses America/New_York and seasonal DST; explicit EST stays UTC-05:00. Unknown
abbreviations require clarification. Ambiguous DST times require `fold`; nonexistent
times are rejected. Conversions preserve the original schedule, applicant-local
date rollover, and pinned tzdata version.

## PDF and WAL maintenance

PDF support is an optional `pdf` extra. JSON is the authoritative editable report
document. Applicant Markdown contains a readable template and integrity-checked
report metadata; edit JSON and re-export Markdown instead of editing only its
visible body. General Markdown and HTML execution are unsupported.

PDF v1 supports Latin text, accents, and currency symbols with the bundled Noto
Sans font. Indic and other shaping-dependent scripts use visible reversible
Unicode escapes. `--missing-glyph error` rejects unsupported text before replacing
an existing PDF. Original Unicode is preserved in JSON/Markdown. The companion
`.pdf.manifest.json` records font/template versions and substitutions. No claim of
Indic shaping is made merely because glyphs exist in a font.

```powershell
uv run applicant store checkpoint --database applicant.db
uv run applicant store checkpoint --database applicant.db --mode truncate --json
```

All persistent connections use a 5000ms busy timeout; writable stores retain WAL
and automatic checkpointing at 1000 pages. Rendering occurs after read transactions
end. Checkpoint maintenance never creates/migrates a database. PASSIVE can return
partial progress with busy=0 when readers retain frames. TRUNCATE is explicit and
returns a retryable failure if it cannot complete. Never delete WAL/SHM sidecars.

## Validation

See [research repair coverage](research-coverage.md) for command-level regression
coverage, precise corporate event imports, session recovery, and worker lifecycle
contracts. CI enforces separate 90% line and branch floors for the five research
modules identified in the coverage review.

```powershell
uv run --extra pdf pytest tests
uv run --extra pdf pyright
uv run --extra pdf coverage run -m pytest tests
uv run --extra pdf coverage combine
uv run --extra pdf coverage report
uv run --extra pdf python tools/smoke_research.py
uv run --extra pdf python tools/probe_research_providers.py --export docs/research-provider-validation.json
```

Required tests are offline: unittest under pytest, mock HTTP/browser dependencies,
temporary SQLite stores, injected clocks, and Hypothesis properties. Live probes
are explicit operational checks and never required CI. Synthetic two-board
fixtures do not satisfy the separate live cross-board acceptance gate.

### Live release checklist

1. Select two currently readable listings that may share an employer requisition.
   Use canonical provider IDs and HTTPS listing URLs without query strings.
2. From the default branch, dispatch **Live requisition release check** with the
   release commit SHA and candidate JSON. The commit must be reachable from the
   default branch. Use public access by default. For an explicitly provisioned
   LinkedIn session, set `LINKEDIN_STORAGE_STATE` in the `live-requisition`
   environment and enable the session input.
3. Download `live-requisition.json` from the run. With the release commit checked
   out, run the artifact check below. Record the successful run and artifact link
   in the release checklist. Eligibility expires after 24 hours. A blocked or
   failed result leaves the release gate pending.

```json
{"listings":{"builtin":"https://builtin.com/job/example/123","wellfound":"https://wellfound.com/jobs/456-example"}}
```

```powershell
uv run --frozen python tools/probe_research_providers.py --candidate candidate.json --export live-requisition.json
uv run --frozen python tools/probe_research_providers.py --release-report live-requisition.json
```

The artifact records both acquisitions and their match revision. It discloses
direct, agency, and unknown origins and the available syndication evidence.
Only confirmed employer requisition identity qualifies the pair. Candidate probe
exit codes are 0 for verified, 3 for blocked, 1 for failed, and 2 for invalid input.
