# Research repair acceptance matrix

The research gate checks line and branch coverage separately (90% each) for
`cli/setup.py`, `cli/sources.py`, `services/provider_setup.py`,
`services/research_feed.py`, and `domain/research_analytics.py`. Existing global
and Milestone 4 gates remain enabled. No research runtime exclusions are approved.

| Public behavior | Regression suite |
| --- | --- |
| Installed/module UTF-8 entrypoints and embedded stream ownership | `test_entry_encoding.py`, installed CLI smoke |
| Setup provider/status/import and legacy flags | `test_research_cli_branches.py`, `test_setup_lifecycle.py` |
| Storage validation, cookie warnings, atomic replacement | `test_provider_setup.py`, `test_setup_lifecycle.py` |
| Shared profile contention and process death | `test_session_locking.py` |
| Worker private IPC, exceptions and hung teardown | `test_owned_worker.py` |
| Verification and production acquisition worker bridges | `test_verification_worker.py`, `test_worker_bridges.py` |
| Sources list/show/health/probe | `test_research_cli_branches.py` |
| Feed validation, partial results, leases and infrastructure failures | `test_feed_preflight.py`, `test_acquisition_lifecycle.py`, `test_provider_renewal.py` |
| Corporate event versions, inherited context and local report dates | `test_research_repairs.py`, `test_research_workflows.py` |
| Analytics cohorts, salary exclusions, FX and order invariance | `test_analytics_branches.py`, `test_research_workflows.py` |
| Coverage threshold boundaries and invalid input | `test_research_coverage_gate.py` |
| Child tracing and native coverage data combination | `test_owned_worker.py`, `test_research_coverage_data.py` |
| Manual live release qualification | `test_live_requisition_gate.py` |

## Operational contracts

Session files and adjacent verification metadata remain private. File-only setup
has no SQLite health rows or leases. In SQLite mode, setup accepts `--database` and
recovers an existing `needs_setup` row only after verified authentication. It does
not initialize or migrate databases. Session installation survives health recovery
failure; repeat `setup status --check` against the same database. Explicit database
selection conflicts with `--store files`.

Lock order is input validation, profile lock when credentials are used, then
acquisition lease. Session-backed LinkedIn operations retain the same normalized
OS lock as setup through browser teardown. Public guest acquisition does not read
or lock those credentials. Lock contention reports `profile_busy`.

Acquisition leases last ten minutes and renew every 60 seconds. Renewal retries
SQLite contention once after an interruptible one-second delay; each connection
retains its 5,000 ms busy timeout. Background exceptions are relayed to acquisition.
Renewal shutdown waits at most twelve seconds. Abandonment updates only the owned
token and does not change provider failure counters. Process death leaves a lease
eligible for recovery after expiry.

Authentication and production feed workers exchange state through private pipes.
Headless verification has a 60-second deadline; interactive MFA waiting is explicit.
Teardown has a five-second grace period and five-second forced cleanup period.
Windows uses an owned Job Object and POSIX uses a dedicated process group. Cleanup
never targets an unrelated browser. An unconfirmed cleanup retains the profile
lock for the lifetime of its owner process.

## Time and money

V1 corporate event payloads retain their canonical representation and hashes.
V2 import templates accept precise offset-bearing `effective_at`, or a date and
optional IANA `effective_timezone`. Timestamp and date are mutually exclusive.
Dates without timezone use conservative UTC -12/+14 bounds. Evidence inside an
uncertainty interval, without a source date, or overlapping the report cutoff is
timing-indeterminate. Applicant timezone selects the report cutoff; it does not
change event chronology. Predecessor evidence retains its original ownership.

Analytics uses explicitly supplied FX only. Missing currency pairs exclude salary
conversion while preserving demand membership. Older selected snapshots disclose
their age; future-effective snapshots are invalid. There is no nearest-date or
network fallback. New snapshots identify `research-analytics-2` in
`policy_version`. This policy uses exact salary arithmetic and half-even cent
rounding for reported amounts, and chooses equivalent compensation provenance by
the lowest evidence ID. Reports disclose the stored policy. Replay reads the
original bytes and membership for either policy.

## Residual branch register

The [residual coverage review and commit plan](research-residual-coverage-plan.md)
maps the remaining branches to concrete test scenarios and distinguishes
defensive invariants from missing production-worker coverage.

The generated `research-coverage.json` lists residual arcs for the five gated
modules on each platform. Linux and Windows independently evaluate the floors.
The combined report checks source and configuration digests before unioning arcs.
Clean worker exits contribute coverage. Force-killed workers are verified by
parent assertions, including descendant exit and unrelated process survival.

| Boundary | Classification | Verification |
| --- | --- | --- |
| `provider_setup.py:190→191`, inline verifier with an incomplete adapter | Defensive invariant behind public preflight | Setup rejects unsupported providers before dispatch. |
| Feed cleanup with no constructed adapter | Reachable constructor failure | Acquisition tests assert token release and provider outcome. |
| Renewal interruption and failure to persist abandonment | Infrastructure failure | Controlled waits, stale-token isolation, bounded shutdown, and expiry diagnostics. |
| Worker cleanup on the other OS or after forced termination | Platform or instrumentation limitation | Native capability probes and synchronized process tests on both hosts. |

The feed regressions cover canonical URL keys, missing capture methods, partial
results after renewal failure, and malformed observations alongside valid jobs.
The previous ambiguous client-cleanup condition now checks whether a client was
constructed, and the tests exercise both outcomes.

`APPLICANT_REQUIRE_NATIVE_CLEANUP=1` makes a restricted capability probe fail
the native CI jobs. Outside CI, the probe reports a skip only for recognized OS
restrictions. Missing readiness messages fail with the relevant lifecycle phase.

```powershell
uv run --extra pdf coverage run -m pytest tests
uv run coverage combine
uv run coverage json -o coverage-m4.json
uv run python tools/check_milestone_coverage.py coverage-m4.json
uv run python tools/check_research_coverage.py coverage-m4.json
```

Windows subprocess tests override inherited `PYTHONUTF8` and `PYTHONIOENCODING`
to exercise CP1252 and UTF-8 deliberately. Required CI remains offline, including
saved-page browser tests. The live cross-board requisition gate remains pending;
synthetic fixtures and coverage percentages cannot satisfy it.
