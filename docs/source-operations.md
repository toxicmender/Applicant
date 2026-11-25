# Provider operations

Run `applicant sources list --json` or `applicant sources show otta --json` to
inspect the version 1 operation catalog. The [JSON Schema](source-operations.schema.json)
describes the added fields. Existing provider fields and the four default search
boards retain their previous contract.

Routes have stable IDs and numeric priorities. An explicit `--route` pins a
route. Otherwise the eligible route with the lowest priority value wins.
Source health, access restrictions, the destination, and credentials can make
a route ineligible. A live network request cannot silently switch to a file
import. Credential-dependent providers may restart publicly after confirmed
sign-out when the catalog declares that transition.

```powershell
applicant sources probe --provider weworkremotely --operation job_search --json
applicant sources probe --provider builtin --operation job_detail --input detail.json --json
applicant sources probe --provider levels --operation compensation --route import --store files --json
```

For job detail, `detail.json` contains a single `url` field. For an import
probe, supply a provider import document with `--input` to validate it offline.
Omit the file to check the synthetic sample. Import probes report only the
validation outcome and leave the store untouched.

| Typed probe code | Outcome |
|---:|---|
| 0 | Completed, including a recognized empty search or a valid import |
| 1 | Operation, persistence, or process cleanup failed |
| 2 | Invalid input or unavailable route |
| 3 | Authentication setup required, provider block, or temporary throttle |
| 5 | Unrecognized response |
| 6 | Rejected API credentials, insufficient scope, or account quota |
| 7 | Transport or upstream service failure |
| 8 | Coordination or database busy |
| 11 | Browser verification challenge |
| 130 | Interrupted |

The legacy probe returns 0 when it obtains jobs and 1 for an empty search or
operational failure. Both forms include the underlying `operation_exit_code`
and `reason`. Codes 4, 9, and 10 are used by other CLI commands.

## Provider imports and evidence

A provider import contains an existing research `bundle` and a `manifest`
with `provider`, `operation`, `dataset`, `acquired_on`, `rights_basis`, and
`destinations`. For sources requiring written permission, use
`rights_basis: written_permission` and add a local `agreement_reference`.
The reference is used for input validation. It stays out of exported evidence.
Run `applicant research import --from-file FILE --dry-run` before publishing.
The shared research store supports portable reports. Publishing a provider
bundle there requires both `persist` and `export` rights. A display-only
bundle can be validated through the offline probe.

For compensation, the optional top-level `records` map is keyed by the incoming
evidence ID. Its entries may provide a `source_record_id`, `row_locator`, and
`metric`. Without a stable source ID, the import uses a manifest
`source_batch_id` or the canonical bundle digest plus row position. Distinct
rows with identical pay remain distinct. Source revisions preserve bonus,
grant, vesting, valuation, and percentile details where supplied. The source
amount stays in its original currency.

An explicit feed build from imports incorporates later requisition evidence
available in the store. It retains each posting's latest source observation
and identifies older observations used to verify requisition identity. Existing
frozen feeds retain their original match revisions.

## Health and rollout

SQLite schema 15 adds per-route parser quarantine. An adapter revision change
allows a recovery probe once the provider lease and cooldown permit it. A
successful probe clears the quarantine for that route. A parser failure
quarantines the revision used for the attempt. A live pass becomes stale after
30 days.

A feed worker's results become eligible for publication after confirmed
cleanup. If cleanup is unconfirmed, the command exits 1 and reports which
siblings committed. The combined output file is left in place.

The public adapter now filters before limiting matches and validates redirect
targets before following them. Arc and Welcome to the Jungle live extraction
require permission under their published terms. Instahyre remains unverified
after the recorded block. ZipRecruiter, Upwork, and Levels live research are
pending access and destination validation. We Work Remotely account setup
still needs a signed-in Job Seeker control. Public RSS is a separate operation.

- [Arc terms](https://arc.dev/terms)
- [Welcome to the Jungle terms](https://www.welcometothejungle.com/en/pages/terms)
- [Recorded live research validation](research-provider-validation.json)
