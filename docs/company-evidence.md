# Verified company evidence

Milestone 4 phase 8 adds immutable company associations and captures in database
schema 7. Upgrade existing databases explicitly before historical reads:

```powershell
uv run applicant db upgrade --database applicant.db
uv run applicant company-evidence associate --from-file association.json --dry-run
uv run applicant company-evidence associate --from-file association.json
uv run applicant company-evidence capture --association glassdoor-acme --export capture.json
uv run applicant company-evidence refresh --association glassdoor-acme
uv run applicant company-evidence show --association glassdoor-acme --snapshot SNAPSHOT_ID --export evidence.json
uv run applicant company-evidence import --from-file evidence-import.json --dry-run
uv run applicant company-evidence import --from-file evidence-import.json
uv run applicant company-evidence enrich --from-file selections.json --export enriched-jobs.json
uv run applicant rank create --provenance provenance.json --job-versions enriched-jobs.json --offline --json
```

Every command accepts `--database PATH`. The default uses the configured SQLite
database. Commands always emit strict canonical JSON; `--json` is accepted for
consistency. `show` and `enrich` are offline and read-only. They never create a
database, run a ranking, change profiles, or refresh a source. With `--no-log-file`,
they also avoid application log writes. Exports use the existing private atomic
writer and cannot replace the database or its WAL/SHM sidecars.

## Associations and exact identities

An association is a candidate's explicit verification assertion, not a company
search result. Use the exact job identity/version from a persisted ranking run:

```json
{
  "kind": "company_association", "schema_version": 1,
  "association_id": "glassdoor-acme",
  "job_id": "EXACT_JOB_ID", "job_version_id": "EXACT_JOB_VERSION_ID",
  "provider": "glassdoor", "provider_company_id": "42",
  "locator": "https://www.glassdoor.com/Reviews/Acme-Reviews-E42.htm",
  "verified_at": "2026-11-04T00:00:00Z", "verified_by": "candidate",
  "verification_note": "Posting employer and provider profile refer to the same company.",
  "verification_url": "https://example.com/company"
}
```

The example uses placeholders and synthetic identities. Association IDs are
caller-supplied and retry-safe. Changing verification or company identity requires
a new association ID. Associations reference the exact immutable job version;
they never transfer automatically to later versions. Foreign keys enforce the
job identity/version relationship. Names and title similarity are not evidence
of company identity.

Supported locators:

| Provider | Explicit locator and company ID |
|---|---|
| Glassdoor | Canonical `https://www.glassdoor.com/Reviews/SLUG-Reviews-EID.htm`; numeric employer ID must match the URL. |
| AmbitionBox | Explicit lowercase company slug; company ID is the verified returned provider ID, independently checked after acquisition. |
| Crunchbase | Canonical `https://www.crunchbase.com/organization/PERMALINK`; company ID is that exact permalink. |
| Tracxn | Exact 24-character entity ID for API acquisition, or an exact `https://tracxn.com/d/companies/SLUG/__PROFILE` URL for browser acquisition. Company ID must match the structured returned ID; it may differ from the URL's profile token. |

Tracxn entity-ID acquisition requires an API key. An explicit profile locator
always selects the browser, even if a key is configured; it never invokes a
company-name search. Crunchbase uses a full organization URL to prevent its
legacy autocomplete fallback. Glassdoor and Crunchbase capture require matching
structured organization IDs. Tracxn capture rejects missing/mismatched records
instead of using its legacy name fallback. AmbitionBox's returned ID is checked
against the association; a JSON-LD summary without a provider ID cannot qualify.
Missing or conflicting IDs fail capture. Ordinary reviews/financials commands
retain their existing behavior; these stricter guards apply to evidence capture.

## Capture, dates and partial failure

`capture` and `refresh` append new snapshots. Refresh never changes an old capture
or implicitly selects the newest one for enrichment. Repeat `--association` to
request several providers/associations. Each outcome is reported. A failed source
returns a failed outcome and overall exit 1; successful snapshots remain usable.
Unknown association IDs fail preflight before any provider work. Store and payload
validation happen after provider work completes, in a short write transaction.

The four existing clients supply their usual shared HTTP retry/pacing and browser
behavior. Financial clients accept `--crunchbase-key`/`--tracxn-key`, or their
existing environment settings; browser profiles use configured directories.
Capture requests zero individual reviews and at most 20 funding rounds; this
does not assert that a returned round list is the provider's complete history.
There is no implicit interactive login in the new command. Existing provider
login commands can prepare profiles separately. Acquisition can be blocked by
provider access, browser availability, plan restrictions or incomplete identities.
The capture does not claim live-provider compatibility merely because fixtures pass.

Snapshots retain provider/company ID, association ID, acquisition time, optional
source observation time, an observation-date note, adapter version, normalized
provider payload, and raw response envelopes with source URLs and SHA-256 hashes.
Each envelope identifies its representation: exact HTTP response bytes, exact
browser response bytes, or a UTF-8 serialization of the rendered browser DOM.
DOM snapshots are explicitly distinguished from wire responses. Query strings,
URL user information, headers and cookies are not archived as request metadata.
Raw limits are 8 MB per response, 16 MB total and 64 distinct responses per capture.
Limit failures remain fatal even when a browser callback suppresses exceptions.

The legacy clients do not provide a verified aggregate observation timestamp.
Live captures therefore default to **unknown source observation date**. Individual
review dates and financial transaction dates are not reused as aggregate freshness.
Financial `fetched_at` remains acquisition metadata. A dated strict import can
record a caller-verified observation date and its explanation. Observation time
cannot follow acquisition; acquisition cannot precede association verification.

Legacy normalized floating-point values and imported Decimal values use canonical
decimal strings in the snapshot payload. This preserves their stated values and
retry identity, without inventing source precision. Exact raw bytes remain
separate. Counts remain integers. Strict provider imports reject unknown fields,
nonfinite values, invalid categories/counts and mismatched company identities.
Legacy financial clients can report unavailable subresources in their `notes`;
those gaps remain visible rather than becoming zero amounts or completeness claims.

## Strict imports and enrichment

Imports have this envelope:

```json
{
  "kind": "company_evidence_import", "schema_version": 1,
  "associations": [], "snapshots": []
}
```

Supply complete association/snapshot objects in the arrays. A snapshot has
`kind=company_evidence_snapshot`, `schema_version=1`, `snapshot_id`,
`association_id`, `provider`, `provider_company_id`, aware UTC `acquired_at`,
nullable `observed_at`, `observation_note`, `adapter_version=company-capture-1`,
`payload`, and nonempty `responses`. Each response has `source_url`,
`body_base64`, `body_sha256` and `representation`. Imports are caller assertions;
they do not independently authenticate upstream bytes. Duplicate JSON keys and
unknown envelope fields fail. Identical IDs can be retried, while conflicting
reuse fails. A batch imports associations and snapshots atomically. Dry runs
require existing parent job versions and never create/upgrade a database.

Enrichment selections are explicit:

```json
{
  "kind": "company_enrichment", "schema_version": 1,
  "selections": [
    {"association_id": "glassdoor-acme", "snapshot_id": "EXACT_SNAPSHOT_ID"}
  ]
}
```

Selections must match the snapshot's association. Duplicate pairs, multiple
parent versions of the same job, or conflicting company IDs for one provider/job
version fail. Several source snapshots can be retained; disagreements are visible
with their separate source IDs and dates. No newest-capture selection is inferred.

Enrichment exports a standard `JobVersionBundle`. It preserves parent posting
content and evidence, appends self-contained company context with parent-version,
association and snapshot/hash references, and uses a deterministic new version ID.
Repeating identical selections produces the same content/version identity.
Acquisition dates are context metadata, not substituted source observation dates.
Only ratings with a known observation date pass through `capture_rating` into
typed ranking facts. Existing category/count/age/priority policies then apply.
Unknown dates remain context, stale ratings remain unscored, and missing counts
retain the existing reduced-confidence policy. Financial amounts, funding and
valuation remain dated context; they never produce a financial-health score.

Ranking enriched exports uses the ordinary CLI. Existing `ranking-1`, run/profile
formats, eligibility/conflict policies and archived versions remain unchanged.
Ranking JSON reports add company context only for enriched jobs, omitting raw
body blobs from the compact display. The explanation/export retains the full
evidence and provenance. Existing high-quality posting conflicts remain governed
by the original review-required policies. Old runs still replay using old inputs.

## Persistence and validation

Migration 7 appends two immutable tables with composite foreign keys, indexed
references, update/delete/replace guards, and decoded payload/key/hash checks.
Each complete snapshot is one self-contained row, so a single atomic insert
publishes it; there are no separately extensible snapshot children.
Tests exercise upgrades from schemas 1–6, migration and batch rollback, retry
idempotency, wrong IDs, immutable SQL, corrupted history, partial acquisition,
actual provider HTTP/browser capture paths on fixtures, stale/unknown dates,
missing counts, cross-currency financial context, historical replay, Unicode
paths and private atomic exports. CI never needs live company access.

On 2026-11-04, a separate public AmbitionBox smoke request decoded a response
and retained one raw HTTP envelope. This checked client compatibility only;
it did not create or independently verify a job/company association. Live
Glassdoor, Crunchbase and Tracxn validation was unavailable in this run: the
browser was not installed and authenticated API journeys were not attempted.
All four capture paths passed offline fixtures. Hosted CI remains unverified.
