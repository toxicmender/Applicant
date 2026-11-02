# Milestone 3: survey and ranking CLI

Status: phases 1–3 delivered locally; ranking CLI phases pending.
Reviewed 2026-11-02 against `d375842` on `codex/ranking-foundation`.
This document supersedes the M3 command sketch in section 17 of
[Implementation_plan.md](../Implementation_plan.md). M2 library behavior remains
defined by [its plan](milestone-2-plan.md).

## Evaluation

The engine is ready to integrate. The original CLI sketch is insufficient to
implement directly: it leaves profile concurrency, job selection, refresh,
history selection, artifact provenance, and output contracts undefined. The
following decisions resolve those gaps without changing scoring policy.

| Repository evidence | Consequence for implementation |
| --- | --- |
| `cli/__init__.py` registers argparse handlers; shared flags work before/after commands | Add `cli/survey.py` and `cli/rank.py` through the existing registration pattern. Retain handler patch points and exit conventions. |
| `services/preferences.py` already appends revisions with compare-and-swap | Reuse it; capture the expected revision before prompting. Never reload the head and silently overwrite a concurrent edit. |
| Profile v2 adds actual work country and required modes; survey version remains 1 | Emit v2 profiles, survey version 1. Explain required versus preferred modes. Do not change old validators or historical payloads. |
| `Store()` creates directories, changes permissions, enables WAL and migrates | Add an explicit read-only opener before implementing show/export/history/replay. A mere existence check followed by `Store()` is insufficient. |
| `Store.job_items()` synchronizes files and can replace listing rows or restore a deleted export | Do not use it for read-only commands or preflight. Introduce a non-mutating listing snapshot reader. |
| `read_document()` treats malformed JSON as empty; `jobs_from()` supports tolerant legacy loading | Ranking imports must instead fail with field/index errors; a corrupt listing must not become an empty successful run. |
| `JobVersionRepository` exposes get/put, without a latest-version selector | Freeze the chosen listing or explicit version bundle. Never select an arbitrary latest version from all history. |
| `rank_jobs()` needs exact profile revision, build identity, lock digest and one version/job | Resolve and validate these before opening a writer. Use the actual deduplicated versions returned by M2. |
| `RankingRunV3` freezes tier choices; older run formats remain readable | Show historical formats honestly; verification supports only the existing frozen-inputs engine contract. |
| Persisted criteria contain conflict assertions, excerpts and verification prompts | Render those directly. Per-route diagnostic wrappers are not persisted; do not claim they are part of a historical result. |
| Preferred regions/timezone are not evaluated; ordinary `Job` records lack typed routes, technologies and verified review associations | Disclose unavailable signals. Do not infer worldwide permission, extract skills from titles, or join ratings by company display name. |

## 1. Command contract

Both commands require the SQLite backend, including file-validation modes. Reject
`--store files` with exit 2 and a clear explanation before opening a database.
Keep existing search/apply/file-backend commands unchanged. All explicit file
paths use `Settings.path()`: relative to data_dir, absolute paths accepted.

Add `--database PATH` to these two commands only. Default to `applicant.db` beside
the resolved configured listing, matching `Store.beside()`, including when the
listing is outside data_dir. `--input` changes the selected input, not the default
database. Print the resolved database in human summaries to avoid ambiguity.

### Survey

| Invocation | Behavior |
| --- | --- |
| `survey [--profile ID]` | Interactive create/edit; profile ID defaults to `default`. No implicit selection of another profile. |
| `survey --show [--profile ID] [--revision N] [--json]` | Read current/exact revision. Human summary or canonical archived profile JSON. |
| `survey --export PATH [--profile ID] [--revision N]` | Export an editable preferences document, using atomic private file writing. |
| `survey --from-file PATH` | Validate an editable document, then append exactly one revision using its expected revision. |
| `survey --validate PATH` | Validate the complete editable document without opening a database; no concurrency claim. |
| `survey --dry-run --from-file PATH` | Validate, read the head if present, check concurrency, and print the proposed preferences/revision without saving. |

Show/export/from-file/validate are mutually exclusive. Dry-run requires from-file;
revision is for show/export only; JSON is for show only. From-file/validate use
the document's identity and reject a simultaneous `--profile`. Non-TTY stdin in
interactive mode fails with exit 2 and suggests from-file; never hang in CI.

### Rank

| Invocation | Behavior |
| --- | --- |
| `rank --provenance PATH [--profile ID] [--revision N]` | Create a new complete run from the configured listing and current/exact profile revision; default profile `default`. |
| `rank --input PATH ...` | Select another ordinary listing for a new run. |
| `rank --job-versions PATH ...` | Select an explicit ordered frozen-version bundle instead of a listing. |
| `rank --run RUN_ID` | Display a completed run offline; never recalculate or require provenance. |
| `rank --run RUN_ID --explain JOB_ID` | Explain that run's exact job result and captured evidence. Full board-scoped ranking job ID required, not a board's raw ID. |
| `rank --replay RUN_ID --provenance PATH` | Verify with M2 replay, offline and without writes. |
| `rank --list-runs [--profile ID]` | List completed run IDs/revisions/times, ordered calculated_at descending then run ID ascending. No implicit current-profile filter when omitted. |

New runs accept `--offline`, `--scoring-config PATH` (validated full config v2;
default constructed config v2), and `--tiers PATH` (JSON job-ID to tier map).
Do not expose static FX or arbitrary evaluation timestamps in the initial CLI;
the library continues to support those controlled workflows. `--offline` retains
M2 eligible-cache/identity fallback and diagnostic behavior.

Display flags: `--top N` (positive, default 20 **per group**), `--all`, and `--json`.
Top/all are mutually exclusive; apply them after deterministic sorting separately
to eligible, review-required and excluded groups. Always show total/displayed
counts, including empty groups. These flags work for new runs and --run;
--explain rejects explicitly supplied top/all and bypasses the default top limit.
Replay/list-runs accept JSON but reject top/all/explain and all new-run options.
Run/replay/list-runs are mutually exclusive. No ambiguous latest-run shortcut.
--run rejects all creation options, including provenance/profile/revision,
input/config/tiers and offline; it is already offline. Input/job-versions are
mutually exclusive. Default values must not be mistaken for explicitly supplied
options when enforcing these combinations.

Exit codes: 0 success (including review-only/excluded-only results and empty run
history); 1 empty new-run input, storage/I/O failure, replay mismatch or unsupported
replay environment; 2 invalid invocation/document, missing profile needed for a
new run, or revision conflict; 4 explicitly requested historical run/profile/job
not found; 130 interrupt. Preserve existing source-error mappings. Expected
validation errors must not reach the unexpected-exception traceback handler.

## 2. Editable preferences and interaction

Define a separate strict versioned import/export DTO, not a replacement for
archived profiles:

```json
{
  "kind": "candidate_preferences",
  "schema_version": 1,
  "profile_id": "default",
  "expected_revision": 0,
  "preferences": {
    "experience_years": "5",
    "primary_discipline": "Software engineering"
  }
}
```

Preferences expose all user fields of CandidateProfileV2, with the same defaults
and constraints. Exclude profile/schema/survey/registry identities and updated_at;
the application supplies those. Reject unknown fields, duplicate JSON keys,
nonfinite values, and unsupported document versions. Parse JSON decimal tokens
as Decimal; serialize decimals as strings using existing canonical conventions.
Validate weights in the engine's fixed Decimal context. Reuse model validation
rather than duplicating salary/weight/country rules in the questionnaire.

Expected revision is a strict nonnegative integer (not a boolean). Revision 0
means create; N means append N+1. An export includes all
preferences and the exported revision as expected_revision. Importing an unchanged
export still creates an explicit new revision. Exporting an old revision remains
possible; importing it against a newer head conflicts. No implicit merge, force
overwrite, or ID remapping. Export is an editable document; --show --json is the
archival representation. Accepting archival v1/v2 JSON as editable input is not
part of this format. Editing an existing v1 profile explicitly creates a v2 revision
with the M2 upgrade defaults; historical v1 remains untouched.

Use existing `Interaction`/`Terminal` and `Scripted` test support. Prompts go to
stderr; successful output goes to stdout. Prompt every editable field in a fixed
order, with current/default values, enum choices, and units. Enter keeps a value;
`-` clears optional fields/lists, and is invalid for required fields. Lists are
comma-separated, whitespace-trimmed and deduplicated in input order; reject empty
internal items. Ask the six weights as fractions summing to one, with existing
defaults; do not silently normalize them. Re-prompt invalid fields and cross-field
groups (salary thresholds/weights), then show a summary and ask save yes/no.
Declining or EOF exits without a write; Ctrl-C returns 130. Hold no database
transaction while prompting. The final save uses the originally observed revision.

Explain that salary is annual base pay, gross/net is explicit, country is the
actual work location, required modes constrain eligibility, and preferred modes
affect fit. Experience/discipline/seniority/regions/timezone are retained profile
information but currently do not add ranking signals. Do not imply otherwise.

## 3. Input selection, provenance and transaction boundary

1. Parse options and all documents before writes or network. Read the exact
   profile using a read-only connection, then close that connection before FX.
2. Read listing bytes once if the file exists, honoring the existing external-file
   precedence. Require the actual legacy `{list: [...]}` shape used by
   storage.save_jobs; validate every job and reject
   duplicate derived job IDs. If absent, read the collection's existing SQLite
   rows in seq order using a new query that does not sync/restore the export.
   Permit legacy extra job fields as Job.from_dict does, but never skip malformed
   records. Reject unknown top-level wrapper fields. An empty list is valid input
   with the empty-input exit behavior below; `{}` is malformed.
   Missing file and collection means no inputs. Malformed files never fall back
   to old DB rows. Rank snapshots these jobs but does not synchronize the mutable
   collection or rewrite the listing.
3. Use one aware UTC evaluation timestamp from an injectable service clock. For
   ordinary listings, capture at that timestamp with parser policy
   `cli-listing-1`, explicitly meaning local capture, not a fresh remote fetch.
   Preserve no invented board-observation timestamp. Freeze content excluding
   transient flags as JobVersion.capture does. The resulting run is the audit of
   exactly what was read even if the file changes afterward.
4. Version bundle format: `{kind: "ranking_job_versions", schema_version: 1,
   jobs: [<full JobVersion>, ...]}`. Preserve input order, source times, descriptions
   and typed evidence; validate all elements/versions, duplicates and future
   consumed evidence before writes. Preserve raw job/evidence JSON number types;
   do not apply the preferences decoder's Decimal-to-string conversion to arbitrary
   payloads or change content hashes on bundle round-trip. Typed Decimal fields
   use their canonical string representation in exported fixtures. Strict
   duplicate-key/nonfinite checking still applies. Do not choose the newest
   version by timestamp.
5. Tier file is a strict JSON object with nonblank exact job-ID/tier strings;
   duplicate keys and unknown input IDs are usage errors. A named tier absent
   from that job remains the existing engine review outcome, not a CLI override.
6. Provenance format: `{kind: "ranking_provenance", schema_version: 1,
   build_identity: "<retained artifact ID>", lockfile_digest: "<64 lowercase hex>"}`.
   Required for creation and replay; pass both values to existing services.
   Do not read identifiers from the archived run and echo them as environment
   verification. Do not guess Git HEAD or hash an unrelated cwd lockfile. This is
   caller-supplied metadata, not binary attestation. Document obtaining SHA-256 of
   the retained uv.lock and retaining the corresponding source/release artifact.
7. Empty input exits 1 before opening a writer, resolving FX or creating a run.
   All jobs are evaluated once before display truncation. Open the writer only
   after preflight, call rank_jobs once, close it before rendering. M2 already
   resolves FX outside write transactions and publishes completion atomically.
   Runtime failure can leave immutable inputs but never a partial completed run.

Do not attach existing company ratings automatically: storage keys by company
name do not establish the verified association required by capture_rating.
Typed evidence can enter through version bundles; ordinary listings will often
have low coverage or require review, and the CLI must explain that accurately.

## 4. Reads, exports and presentation

Add `Store.open_readonly(path)` (or an equivalent connection owner accepted by
history services) using an escaped absolute file URI and mode=ro. Reuse repository
decoders/integrity checks. No mkdir, chmod, migration, journal-mode change, listing
sync or write transaction. Check user_version: older stores need a writable
upgrade operation; newer stores are unsupported. A fully validated survey save
may use the normal writer to migrate an older store; schemas without ranking
tables have no existing profile and require expected_revision=0. Return an
actionable error for read-only access to an old schema; never migrate during a
read. Use short read transactions for consistent multi-row
reads, then close before printing. Add completed-run enumeration in infra/store;
never put SQL in CLI or services. No schema migration is expected for M3.
For an absent database, --list-runs returns an empty list, show/export/run/replay
return not-found, interactive survey starts at revision 0, and dry-run permits
only expected_revision=0. Opening failures/corruption must not masquerade as absence.

Read-only means no application/database-content mutation, not zero filesystem
activity: SQLite may need WAL/SHM sidecars and normal CLI logging creates logs.
Do not use immutable=1 on a live DB to suppress WAL behavior. Missing-store
reads create no DB/directories; tests for zero filesystem effects also use
--no-log-file. Exports are intentional writes: reuse atomic write_document and
private permissions, validate before opening output, and reject targets resolving
to the DB or its WAL/SHM sidecars. Preserve an existing export on failure.

Add pure presentation DTO/builders shared by text and JSON. JSON uses a separate
`kind` and output schema_version 1 with run identity, exact profile/config/FX
references, group totals, displayed results and explicit nullable scores. Keep
decimals as strings and eligibility explicit (it is a computed model property).
Use separate versioned envelopes for run lists, explanations and replay reports.
No human progress text in JSON stdout; diagnostics/logs stay on stderr.

Human output shows fit, evidence confidence, and coverage separately. Unknown
score displays "unscored", never zero. Explain confidence as evidence support,
not a hiring probability. Each review/exclusion row includes its reason code and
short explanation. Detailed explanation includes all eligibility criteria,
conflicting source values/excerpts/times/IDs, needs_verification, normalized pay,
FX date/source, selected tier, all six dimension statuses/terms/weights and input
diagnostics. Resolve references only against frozen evidence. Missing source
detail is shown as unavailable, not synthesized. Escape terminal control
characters in posting text; truncate only summaries, not JSON evidence.

Legacy schema-1 runs expose their available stored fields in input order, with
an "eligibility unavailable in this format" label; do not fabricate modern
groups or recompute them. Schema-2/3 runs use ranked_views. Top applies to the
single legacy list. Explanation selects only a member of that run. Ordinary
display never recalculates supplemental route traces with today's code.

## 5. Explicit amendments to proposed flags

| Original flag | M3 decision and reason |
| --- | --- |
| `--explain JOB_ID` alone | Require --run RUN_ID, so an explanation cannot silently choose another profile revision or run. |
| `--region Europe` | Defer. Existing location strings/preferred regions do not define a complete versioned region taxonomy or remote-work geography filter. Do not equate European offices with permission to work throughout Europe. |
| `--refresh-stale` | Defer posting/review refresh. There is no board-observation freshness policy or verified review association pipeline. New runs already evaluate at the new timestamp and use M2 FX resolution; search/enrichment remains explicit. |
| `--force` | Omit. Every normal rank invocation creates a new immutable run, so there is no cached-result bypass to force. It must never bypass evidence conflicts, CAS, or hard eligibility. |

These are deliberate scope amendments, not implemented flags or silently ignored
arguments. Unsupported flags must produce argparse errors. A later region or
refresh feature needs its own versioned selection/acquisition contract.

## 6. Semantic commit sequence

Each commit includes focused behavioral tests and remains independently usable.

1. `feat(store): add read-only ranking queries and listing snapshots`
   Read-only connection ownership/schema checks, exact collection snapshots,
   completed-run enumeration, missing-store handling and cross-platform paths.
2. `feat(survey): add revision-safe preference documents and imports`
   Strict Decimal document parsing, editable DTO, validation/dry-run/import,
   profile display/export and CLI registration; atomic exports and CAS tests.
3. `feat(survey): add interactive candidate questionnaire`
   Scriptable prompts, hard/soft distinctions, cross-field correction, abort and
   concurrent-editor tests; no writes until confirmed valid completion.
4. `feat(rank): create runs from validated CLI inputs`
   Listing/version-bundle selection, provenance/config/tier documents, frozen
   profile revision, offline mode, preflight and engine orchestration.
5. `feat(rank): explain historical results and verify replay`
   Historical display/listing, deterministic grouped text/JSON, conflict details,
   legacy display and offline verification with explicit artifact metadata.
6. `test(cli): cover survey and ranking journeys across platforms`
   Shared golden fixtures, subprocess/installed-wheel journeys and coverage audit;
   update README/CHANGELOG and milestone delivery status with actual results.

## 7. Acceptance and coverage

- Reuse tests/ranking_fixtures.py and existing cross-currency, tier and route
  conflict golden data. Add preferences/provenance/version-bundle and output
  fixtures with fixed clocks/IDs; no live rates, sleeps or current-date snapshots.
- Test global flags before/after commands, invalid option combinations, strict
  JSON/Decimal parsing, unknown schemas, stdout/stderr isolation and exit codes.
- Test invalid imports create no DB and change no existing rows; malformed listing
  fails closed; export failures preserve old content; Windows paths with spaces,
  Unicode, # and ? are correctly encoded where the filesystem permits them.
- Two editors at revision N: exactly one append succeeds. Cancel/EOF/interrupt
  preserve history. Dry-run must not imply success if the expected head differs.
- Whole CLI journey: survey import, rank mixed eligible/review/excluded jobs,
  explain conflict, update profile/listing/config, show original and replay it
  offline. Test mismatch/unsupported replay, corrupt history and completed-only
  enumeration. Assert no provider/clock/profile-head use during historical display.
- Top limits display only; full result membership persists. Equal rounded scores
  retain job-ID tie-breaks. Review groups and unknown/null scores remain visible.
- Many alternative routes continue to use M2 evaluation once/job; no evaluation
  per displayed column or Cartesian route/country product. Keep existing predicate
  count/property tests; add a service call-count assertion, not a flaky time limit.
- Keep branch coverage >=90% overall; inspect new module statement and branch
  gaps individually, covering failure paths rather than relying on M2's high total.
  No new coverage exclusions to meet the floor. Run the complete suite once the
  integrated slice is complete, plus Ruff, Windows/Linux-target Pyright, uv lock
  --check, uv build, installed-wheel CLI smoke and hosted Windows/Linux CI.

Review validation: `uv run --frozen pytest tests/test_cli.py
tests/test_cli_paths.py tests/test_ranking_contracts.py tests/test_ranking_engine.py
tests/test_ranking_history.py tests/test_ranking_run_choices.py
tests/test_store_initialization.py -q` passed **89 tests, 64 subtests**, with one
POSIX-only skip. This was a focused review run, not a new full coverage measurement.
The prior M2 baseline is **1,105 passed / 13 skipped and 98% branch-enabled
coverage**, documented in [ranking-coverage.md](ranking-coverage.md). Hosted gates
remain unverified. No new dependency is needed; use uv for all project tooling.

## Phase 1 delivery

Implemented Store.open_readonly, explicit read_snapshot transactions, stored
listing_items_snapshot queries, strict file-first listing_snapshot selection and
RankingRunRepository.completed enumeration. Existing history/replay services can
use the read-only Store. No schema migration or dependency change was needed.
Read callers own the snapshot context when combining queries. Missing database
and incompatible schemas raise StoreError; the later CLI translates these into
the command-specific empty/not-found behavior specified above.

Validation: **1,115 passed, 13 skipped, 1,073 subtests passed** in the full Windows
coverage run; **98%** overall branch-enabled coverage. The listing snapshot module
and completed-run repository have 100% statement/branch coverage. The existing
SQLite module is 97%, with no uncovered lines in the new read APIs. Ruff,
formatting, Windows/Linux-target Pyright and uv lock --check passed. Browser and
POSIX-only skips remain; hosted CI was not run. Tests cover a concurrent commit
during a read snapshot, immutable database bytes, permission/migration avoidance,
missing files/directories, schema rejection/handle release, malformed imports,
deleted exports, profile filtering, stable ordering and incomplete/corrupt runs.

## Phase 2 delivery

Implemented PreferenceDocument with a strict editable-field boundary and shared
v2 profile validation, Decimal JSON parsing and arithmetic-context isolation.
Added survey --validate, --dry-run --from-file, --from-file, --show (including
--json and exact revisions), and atomic --export. Paths follow Settings;
--database overrides the database beside the configured listing. Validation
precedes writer creation; dry-run and history use the phase 1 read-only APIs.
Expected revision ownership is enforced atomically by the existing CAS service.
ProfileNotFound distinguishes absence from corrupt archived data. Historical v1
exports can create a new v2 revision without modifying their source revision.

At the phase 2 boundary, interactive invocation returned an actionable usage
error. Phase 3 below delivers that questionnaire.

Coverage review: the four new survey modules plus the existing profile repository
have **100% statement and branch coverage: 201 statements, 46 branches**. Added a
corrupt-profile identity case to exercise the existing repository guard; no
coverage exclusions were added. Full Windows validation reports **1,126 passed,
13 skipped, 1,108 subtests passed**, with **98%** overall branch-enabled coverage.
Ruff, formatting, Windows/Linux-target Pyright, uv lock --check and uv build pass.
The installed wheel's survey validation command passed in an isolated offline uv
environment. The skips remain browser/POSIX-only; hosted CI is still unverified.

Tests use a reusable golden preference document and cover precision, hostile
ambient Decimal context, invalid/duplicate-key documents before writer creation,
missing-store dry-run, imports/exports, stale edits, exact historical reads,
v1-to-v2 edits, two concurrent importers (one save and one conflict), protected
export targets, failed atomic replacement, path precedence, exit codes and
corruption that must not become absence.

## Phase 3 delivery

Implemented an Interaction-based questionnaire covering every editable profile
field. Existing defaults are shown; new profiles must supply experience and
discipline. Fields reuse v2 annotations for validation within the fixed arithmetic
context; complete documents reuse the existing model-level validation. Salary and
weight groups can be corrected without losing unrelated answers. Comma lists
deduplicate in input order and reject empty internal items. Enter keeps values;
minus clears optional values/lists. A summary requires explicit yes to save.

The CLI loads the head once and closes the read owner before asking anything.
No writer is opened until confirmed completion; save retains that original CAS
revision. Decline/EOF cancel without a save, interrupts retain exit 130, and
non-TTY stdin returns exit 2 suggesting --from-file. Prompts/feedback stay on
stderr; success/cancellation results go to stdout.

Coverage review: the questionnaire and four existing survey modules have **100%
statement and branch coverage: 253 statements, 62 branches**. Full Windows
validation reports **1,136 passed, 13 skipped, 1,116 subtests passed** in 75.20
seconds under coverage; overall branch-enabled coverage remains **98%**. No
coverage exclusions were added. Tests cover every editable field, valid/invalid
values, required versus optional clearing, list deduplication, salary/weight
group correction, hostile ambient Decimal context, confirmation correction,
decline/EOF/interrupt, concurrent edits during confirmation, the closed reader
before prompts, and actual Terminal stdout/stderr separation.

Ruff, formatting, Windows/Linux-target Pyright, uv lock --check and uv build pass.
The wheel's interactive workflow also passed with scripted terminal answers and
a temporary database in an isolated offline uv environment. Browser/POSIX skips
remain unchanged; hosted CI is still unverified.

Next: phase 4 create ranking runs from validated CLI inputs.

## References checked 2026-11-02

- [Python argparse](https://docs.python.org/3.10/library/argparse.html): retain
  subparsers/mutual exclusion; parse paths as values and open them only after
  validation, avoiding FileType's early file-opening lifecycle.
- [SQLite URI filenames](https://www.sqlite.org/uri.html) and
  [WAL read-only behavior](https://www.sqlite.org/wal.html#read_only_databases):
  mode=ro supports read-only connections; sidecar requirements and immutable
  assumptions must be considered separately.
- [Python JSON decoder](https://docs.python.org/3.10/library/json.html): use
  parse_float, parse_constant and object_pairs_hook for Decimal tokens, rejecting
  nonfinite constants and duplicate keys before contract validation.
- [uv locking and syncing](https://docs.astral.sh/uv/concepts/projects/sync/):
  --frozen uses the lock without checking freshness; retain a separate
  uv lock --check gate. A lock digest identifies a file, not an attested runtime.

Command semantics and scope amendments above are repository-specific design
decisions; these references support implementation mechanics, not product policy.
