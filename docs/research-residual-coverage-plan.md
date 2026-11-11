# Research residual coverage review and semantic commit plan

Reviewed against `fff9052` on 2026-11-11. This is a proposed follow-up plan,
not an implementation record. It evaluates the residual register in
[research-coverage.md](research-coverage.md), the actual test assertions, and
coverage arcs. Preserve the global, Milestone 4, and five separate research
line/branch gates. Do not modify stored research documents, hashes, or snapshots.

## Findings

1. **The existing residual register is too broad to audit.** It groups worker
   paths and extension guards without identifying arcs, tests, platforms, or
   the distinction between missing instrumentation and missing behavior tests.
   The root `coverage-m4.json` predates these repairs and cannot establish the
   current baseline. Record the revision and command with each new artifact.
2. **Production worker coverage is a real gap.** Existing verification tests
   inject a browser factory and bypass `verify_state`'s owned-worker dispatch.
   Existing feed tests inject board factories and bypass `OwnedBoard`. The three
   owned-worker tests establish round-trip, hanging-close, and sanitized-error
   behavior, but do not establish production bridge behavior, interruption,
   startup failure, or descendant cleanup. Missing child-process tracing alone
   cannot explain missing parent-side dispatch and bridge execution.
3. **Renewal failure coverage is thinner than the documented contract.** The
   tests exercise successful renewal and manually invoke the exception relay,
   but do not exercise contention retry, ownership loss, bounded shutdown, or
   abandonment persistence failure. These live in `provider_health.py`, outside
   the five research module gates.
4. **Feed partial preservation is untested on infrastructure failure.** The
   existing lease-failure test fails before collecting jobs. It never asserts
   preservation after a successful capture followed by `LeaseFailure` or
   `WorkerFailure`, or health/token state after persistence or cleanup failures.
5. **Analytics branch saturation misses two reproducible defects.** Calling
   `analytics` with the standard fixture under Decimal precision 28 produces
   median `112500`. Precision 3 produces `1.12E+5` with `ROUND_DOWN` and
   `1.13E+5` with `ROUND_UP`. Annualization and median arithmetic occur outside
   the explicit conversion context. Reversing the fixture's evidence leaves
   rows equal but changes member `compensation_evidence_id` values because
   equivalent candidates overwrite each other in traversal order. The existing
   order-invariance test checks rows only. Both need regression fixes even when
   line and branch coverage are 100%.

Prioritize the acquisition/worker ownership tests as P1 because they protect
credentials, processes, and durable partial results. Treat the two reproduced
analytics defects as P2 correctness fixes. Defensive guards and instrumentation
cleanup follow those behavioral gaps.

## Arc disposition

Line references below refer to `fff9052`. The prior local coverage database is
useful for locating candidates, not for asserting final-revision percentages.
The fresh baseline at the end of this document supersedes stale counts.

| Module and location | Classification | Action |
| --- | --- | --- |
| `provider_setup.py:33-36`, file-only recovery | Reachable service behavior, already exercised in `test_setup_lifecycle.py` | Re-measure before adding tests. Extend to setup/status only where assertions are absent. Do not duplicate the existing import test. |
| `provider_setup.py:159-183`, default verifier dispatch and worker IPC | Reachable production behavior plus child instrumentation | Test parent dispatch, the actual verification worker protocol, and a spawned offline journey. |
| `provider_setup.py:190-191`, inline unsupported adapter guard | Defensive invariant behind public adapter preflight | Retain and register the reason. Do not mutate the provider registry into an impossible state just to hit this branch. |
| `research_feed.py:69-70`, unselected/noncanonical URL provider | Reachable input validation | Exercise canonical provider selection, alias misuse in URL keys, and unselected URL keys before any resource acquisition. |
| `research_feed.py:99-100`, missing callable capture adapter | Reachable extension-contract failure | Supply a minimal adapter without `capture`, then a noncallable `capture`, through the existing factory seam. Assert outcome, cleanup, and token release. |
| `research_feed.py:116-119`, completed jobs on infrastructure failure | Reachable operational failure | Fail the second capture or the post-request ownership check; assert durable observations and unchanged provider failure counters. |
| `research_feed.py:164-167`, false client after normal acquisition | Defensive normal-flow edge | Constructor failures already exercise cleanup with no client. A successful acquisition cannot normally have no client. Retain a documented residual or simplify ownership; do not manufacture a false-valued client. |
| `provider_health.py:170-179`, bounded renewal retry | Reachable infrastructure failure plus unreachable loop exhaustion | Test busy-then-success, exhausted busy, interrupted backoff, and immediate nonbusy failure. The loop cannot normally exhaust because its last attempt returns or raises; simplify or register that residual. |
| `provider_health.py:38-52, 109-110, 132-133, 198-199` | Reachable recovery, server-delay, ownership, and shutdown failures | Inject failures at connection/renewal boundaries and assert public outcomes plus database state. |
| `owned_worker.py`, `board_worker.py`, `profile_lock.py` | Native lifecycle and adapter protocol | Separate platform-specific arcs, child-only execution, and genuinely missing tests. Add real subprocess assertions alongside bounded boundary fakes. |
| `cli/setup.py`, `cli/sources.py`, `research_analytics.py` | No residual arcs in the prior database | Preserve their gates. Add tests for meaningful behavioral gaps, not a numerical target above 100%. |

## Proposed semantic commits

Every fix lands with its regression tests. If an intended test exposes a defect,
use `fix` for that commit and repair the behavior before landing it. No expected
failures or blanket coverage exclusions. Use `unittest.TestCase` under pytest,
`unittest.mock` at owned boundaries, and Hypothesis for meaningful permutations
and sequences.

### 1. Acquisition validation

`test(feed): cover canonical URL keys and adapter capture contracts`

Extend `test_acquisition_lifecycle.py` with unselected providers, Otta alias
normalization, noncanonical URL keys, malformed URL lists, and capture-less
adapters. Assert that invalid queries never instantiate `Acquisition`, create a
database, or call a factory. For runtime adapter mismatch, assert `close` once,
the documented failure outcome, no retained lease, and no captured observations.
Preserve the distinction between public preflight and runtime extension failure.

### 2. Acquisition preservation

`test(feed): cover partial results and persistence failure ownership`

Cover successful first URL then lease/worker failure, successful search then
failed ownership check, partial source failure, and empty results. Inspect the
saved feed and reopened observation store, not just the return status. Inject
import, snapshot-save, and client-close failures separately. Require either an
owned-token release or an explicit persistence-failure outcome, no overwrite of
a competing token, and no provider cooldown for infrastructure failure. Extend
the implementation in this same commit if these assertions reveal a defect.

Depends on commit 1's preflight fixture separation.

### 3. Setup and production verification

`test(setup): exercise owned verification dispatch and recovery boundaries`

Call the default `verify_state` path with `run_worker` intercepted and assert
the worker callable, private argument payload, interactive flag, and result.
Exercise `_verification_worker` with a private pipe and browser fake to verify
`pause`, acknowledgement, `closing`, authenticated state, and redirect outcomes.
Add a spawned offline fixture that proves the bridge executes in a child.
Keep credentials out of command arguments and errors.

Extend file-only setup/status assertions only where absent. Assert no health
connection on dry-run, unverified import, failed verification, or file-only mode.
For actual recovery errors, preserve installed state and metadata while checking
retryable exit 8 versus nonretryable exit 1 and the database-specific retry command.
Check missing and incompatible databases without creation or migration.

### 4. Renewable lease ownership

`test(providers): verify renewal retries shutdown and stale-token isolation`

Use injected clocks and controlled events to test one busy retry, two exhausted
attempts, cancellation during the one-second backoff, nonbusy failure without a
retry, unexpected background exceptions, and the 12-second shutdown bound.
Inject renewal-thread startup failure after lease acquisition and verify that
the acquired token is released even when context entry itself fails.
Exercise `renew` and `abandon` against a real temporary database after a competing
token replaces the old one. Assert the new token and health counters survive.
Inject failure in abandonment and verify that expiry is the stated recovery
mechanism. Cover long `retry_at` preservation and busy/corrupt recovery databases.
Do not sleep for lease durations or corrupt SQLite files to manufacture failures.

### 5. Production board bridge

`test(boards): cover owned acquisition IPC and error translation`

Add focused `board_worker.py` tests for search/capture forwarding, raw byte versus
mapping responses, accumulated source and requisition metadata, partial jobs,
all supported source error classes, unknown `SourceError` subclasses, and
`RateLimited.retry_at`. Test client-construction and close failures separately.
Use an importable offline child fixture for one real owned-process journey;
parent-only monkeypatches do not propagate into Windows spawn children.
Wire `collect_feed` through its default factory path and assert cancellation,
saved provenance, and partial preservation. Reuse commit 2's persistence checks.

### 6. Browser teardown and shared credentials

`test(browser): verify owned-tree teardown and retained profile locks`

Cover process-start failure, ready-handshake failure, unexpected EOF, worker exit
without a result, interactive acknowledgement, interruption in the pause callback,
ownership-check failure, timeout, and result followed by a hung process. Assert
pipe/process/job-handle cleanup, bounded completion, and original-error diagnostics.
Inject Windows API failures at the owned API boundary and use native Windows
tests for Job Object descendant termination. Use native POSIX tests for process
groups. Confirm an unrelated sentinel process survives.

In a sacrificial subprocess, raise `CleanupUnconfirmed` while holding a profile
lock. Verify a second process remains blocked until the owner exits. Also verify
setup/import versus session-backed acquisition contention on the same normalized
profile across distinct databases. Assert no health mutation on `profile_busy`
and validate the profile-lock-before-database-lease order. Keep process-retained
handles out of the pytest parent. These tests may expose cleanup/ordering repairs;
land those repairs with the tests rather than approving current behavior.

Depends on commits 3-5's controlled worker and acquisition fixtures.

### 7. Decimal determinism

`fix(analytics): isolate annualization conversion and median decimal contexts`

Move all salary arithmetic, including range midpoint, period normalization and
even-sample median, under an explicit local precision and rounding contract.
Test caller precision, rounding, and trap variations using valid monetary inputs,
with and without currency conversion. Assert exact salaries and medians for
annual/monthly/weekly data, half-even ties, and five versus six samples. Validate
supported magnitude/precision rather than silently rounding inputs beyond the
chosen arithmetic budget. Verify that the caller's Decimal context is unchanged.
Replay of saved snapshots must remain byte-identical; only new calculations use
the repaired behavior and disclose any required metric policy revision.

### 8. Analytics provenance determinism

`fix(analytics): select equivalent salary provenance deterministically`

Choose the representative of identical compensation observations by an explicit
stable key, and canonicalize newly created membership/evidence ordering where
required. Preserve all supporting evidence IDs and conflicting-observation
exclusions. Use Hypothesis permutations to compare the complete generated
document at a fixed creation time, including selected salary IDs and membership,
not only rows. Do not mutate or recalculate persisted snapshots. Review the
metric-policy version with commit 7 so one coherent policy change is published.

### 9. Coverage evidence and residual register

`ci(research): publish revision-bound native and worker coverage evidence`

Publish commit SHA, Python/OS, coverage version, command, test result, and separate
line/branch counts with the arc artifact. Add coverage collection for the native
Windows lifecycle tier alongside Linux. Configure supported multiprocessing
tracing if needed and prove it with a child-only sentinel. Keep child data in
parallel files, combine only compatible same-revision runs, and do not assume
force-killed workers flush coverage. Retain explicit parent assertions for those
paths. Keep the existing required gates independently passing; platform-combined
diagnostics must not conceal a failed per-job gate.

Replace broad residual prose with an arc-level register containing branch,
category, rationale, covering behavioral test if any, native platform, and
follow-up. Document defensive residuals without exclusions. Report supporting
module coverage explicitly before deciding whether to introduce additional gates.
No new floor is silently imposed by this coverage-only follow-up.

Depends on all applicable test commits. Commits 1, 3, 4, and 7 can be developed
independently; commit 8 shares analytics policy work with commit 7.

## Completion checks

- Run focused suites per commit, then the complete offline suite with the PDF
  extra, global coverage, Milestone 4 gate, and separate research module gates.
- Run lint, formatting, Linux/Windows type checks, saved-page browser tests, and
  installed-package journeys. Native Windows process tests and Linux process
  tests must both pass in CI.
- Attach before/after arcs, full behavioral assertions, and the residual register
  to the PR. A passing percentage is not proof of teardown, provenance, or money
  correctness.
- Preserve the separate live requisition-identity gate as pending until actually
  verified. Offline fixtures cannot satisfy it.

## Fresh validation baseline

The full native Windows Python 3.12 run passed **1,311 tests and 1,698 subtests**
with coverage 7.17.0. Thirteen tests skipped: four POSIX-specific checks and nine
saved-page Chromium tests because the configured browser executable was absent.
The focused run separately passed **50 tests and 47 subtests**. Sandbox attempts
failed on access to temporary database/session files and are not used as the
application baseline.

| Gated module | Covered lines | Covered branches | Remaining arcs |
| --- | --- | --- | --- |
| `cli/setup.py` | 65/65 | 26/26 | None |
| `cli/sources.py` | 35/35 | 10/10 | None |
| `domain/research_analytics.py` | 81/81 | 32/32 | None |
| `services/provider_setup.py` | 160/172 | 78/80 | 159→160, 190→191 |
| `services/research_feed.py` | 102/107 | 40/44 | 69→70, 99→100, 116→117, 164→167 |

The table is identical in the focused and full runs. All five separate
line/branch gates pass. File-only recovery arcs
33→34 and 34→35/36 are covered, confirming that those entries in the old coverage
database were stale. The full native measurements for supporting modules
are `provider_health.py` 118/132 lines and 25/34 branches, `board_worker.py` 11/47
and 0/8, `owned_worker.py` 94/129 and 27/54, and `profile_lock.py` 37/42 and 6/10.
These supporting counts are diagnostic, not extra gates. POSIX-only branches
and untraced child execution explain some native-Windows omissions, but not the
unexecuted parent-side `OwnedBoard` bridge.

Global coverage is **97.28% lines**, **92.81% branches**, and **96.25% combined**.
The global floor, Milestone 4 checker, and research checker pass. `git diff
--check` passes. Existing coverage exclusions outside the research modules are
unchanged. No CI, browser install, type check, or installed-package run was
started for this documentation-only review.

The full-run commands were:

```powershell
.venv\Scripts\python.exe -u -m coverage run --data-file=.cache/review-native-coverage -m pytest tests -q
.venv\Scripts\python.exe -m coverage json --data-file=.cache/review-native-coverage -o .cache/review-native-coverage.json
.venv\Scripts\python.exe tools/check_research_coverage.py .cache/review-native-coverage.json
.venv\Scripts\python.exe tools/check_milestone_coverage.py .cache/review-native-coverage.json
```

Local diagnostic artifacts are `.cache/review-native-coverage.json`,
`.cache/review-targeted-native.json`, and their coverage data files. They are
ignored working artifacts; the reviewed revision, test scope, and counts are
retained here. Only this plan and its link in the acceptance matrix changed.
The proposed regression repairs have not been implemented by this review.
