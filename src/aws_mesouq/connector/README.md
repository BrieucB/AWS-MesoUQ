# Foreground publishing connector

Run this connector in a foreground session on your own macOS or Linux machine,
using your own HPC SSH account and an existing verified SSH configuration alias.
The browser application uses invited-account MFA/OAuth; credentials remain in
memory. A private local journal records leases, event identities, submission
uncertainty, output bindings, and archive transfer recovery. Keep the journal
between sessions. A second cloud identity or changed endpoint/site/storage
configuration cannot reuse it. Local cluster mode requires institutional approval;
this project does not assume login-node daemons are permitted.

## Enrolled application-device identity

Before the first session, run `aws-mesouq-connector --journal /private/local/session
--show-archive-public-key` on your own machine. This offline command creates a
local Ed25519 application-device key and prints only its connector ID and public
key. An operator enrolls that public key through IAM under the invited identity's
`connector_public_keys[connector_id]`; browser HTTP cannot register keys. The
scientific model registry separately pins `approved_adapter_sha256` to the trusted
shipped adapter. A missing or changed pin fails before remote staging/submission.

The local journal directory is `0700`; its file containing the device private key
is `0600`, owned by this user, and cannot be a symlink or hardlink. Keep this
journal on the local machine. The device private key is never copied to HPC,
uploaded to AWS, or printed. It grants no SSH or AWS access. The connector signs
successful scientific results and archive receipts with versioned canonical
Ed25519 envelopes bound to the job, project, owner, connector, and event. Browser
OAuth credentials alone cannot forge those events. Retries retain the signature
and event ID across lease rotation. This protects the enrolled connector boundary;
the operator still trusts the enrolled device and account to verify HPC storage.

## Separate computation and retention

IT4I computation belongs on SCRATCH. Provision an existing canonical directory
owned by your account with mode `0700`, for example
`/scratch/project/PROJECT/user/aws-mesouq`. Pass it as `--run-root`.

Durable retention belongs on separate managed PROJECT storage, for example
`/mnt/projN/PROJECT/user/aws-mesouq-archive`. Provision that existing canonical
owner-only `0700` directory and pass it as `--archive-root`. The roots must not
be equal, nested, or reached through symlinks. `--durable-archive-root` is an
operator attestation that PROJECT provides the required durable retention; it
never declares SCRATCH durable. Confirm the real storage allocation and policy
with the operator before enabling that flag.

On completed jobs the connector copies retained cloud inputs, the explicit
`publication_files`, and private execution provenance to a unique per-job
PROJECT directory. It copies no unknown native generation folders. Each bounded
remote helper checks owners, modes, regular files, and symlinks, hashes source
bytes, installs without overwriting existing files, fsyncs, reopens, and verifies
the durable bytes. Journaled partial transfers resume idempotently; changed
bytes fail closed. Only then can a Karolina `archive_verified` receipt identify
the PROJECT directory and the exact uploaded output and retained input IDs.
Private execution manifests and the shipped adapter stay private.
Every stage after a durable content binding can resume from verified PROJECT
bytes, including lost success acknowledgements and interruptions before receipt
journaling; copied files no longer require their SCRATCH sources. Pending events
reverify durable bytes before replay.

The service starts the minimum 30-day AWS artifact retention clock from its own
receipt time, and retains database summaries. A missing attestation, incomplete
copy, or checksum mismatch cannot authorize artifact expiry. Vega uses the same
site-neutral connector configuration and execution guards, but v1 does not assert
a Karolina archive from Vega; live Vega validation is deferred.

## Invocation

Install this package locally and use `aws-mesouq-connector --help`, or run
`scripts/platforms/hpc/run_publishing_connector.py --help` with an explicitly
selected supported interpreter. Select `--site karolina` or `--site vega`, or
matching `MESOUQ_SITE`. Supply application API/OAuth settings, project, expected
HPC user and hostname, SSH alias, trusted model registry, scientific checkout,
SCRATCH run root, and private local journal. Add the separate PROJECT archive
configuration only after operator verification. `--once` processes one bounded
sweep of at most ten distinct jobs; the normal session polls until stopped.

The trusted scientific engine remains in the existing HPC MesoUQ checkout.
The connector snapshots its shipped standalone `adapters/indentation.py` into
the private run, hashes it in the journal and execution manifest, and runs that
snapshot using the trusted scientific Python with the existing checkout as cwd
and PYTHONPATH. It does not require installing this new package on the cluster.
A trusted runtime JSON can pin `python_bin`, `environment_script`,
`environment_sha256`, and `site_runtime_root`. Karolina defaults to
`/usr/bin/python3.11`; never invoke its bare system Python for these workflows.

A trusted scheduler JSON supports `partition`, `account`, `qos`, `gres`,
`cpus-per-task`, `time`, and `mem`. Submission always fixes one node/one task,
`--no-requeue`, and `--export=NIL`, permits at most sixteen CPUs and one GPU,
and checks the actual CPU allocation before scientific work. Lost submission
acknowledgements reconcile the unique own-user scheduler marker and never issue
a second `sbatch`. Ambiguous scheduler identity requires operator reconciliation.

## Benchmark execution-context upgrades

Keep the existing enrolled journal, device key, and original --operator-config.
An upgraded SDK can read a separate trusted registry from
AWS_MESOUQ_BENCHMARK_CONTEXT_REGISTRY; this also works when an existing launcher
constructs BenchmarkWorker directly. The registry must be a canonical absolute
path to an owner-only 0600 regular file, with no symlinks or additional hard links.
Its exact fields are schema (aws_mesouq.benchmark_context_registry.v1),
legacy_operator (the unchanged complete original operator), and contexts
(a list of one to fifteen new entries). Each entry has the full validated operator
and runtime_clock_policy: allocation_deadline_v1. All entries share the verified
HPC account and login host. The new deadline policy requires the new bundled
adapter; it cannot be assigned to the legacy adapter.

The backend freezes an operator execution-context ID in each new campaign.
The SDK advertises only its trusted context IDs in its signed claim, before
the server grants a lease or reserves computation. The complete unsigned claim
or refresh body is journaled before transmission and reused after an ambiguous
response. A pending request created before the upgrade retains its original
selector-free body and request ID.

Normal SDK startup validates and atomically migrates only the benchmark journal
namespace. Flat stage/campaign records, aggregate concurrency, submission markers,
attempts, pending events, upload reservations and archive event IDs remain in
place. The complete legacy context is retained verbatim and each record gains
an immutable context binding. The version marker makes an old SDK refuse this
namespace. Keep all referenced operators in the external registry; removing the
registry or changing a stored context fails closed. Do not hand-edit or reset the
journal to downgrade a binary.

Legacy stages retain their original adapter, script and source/environment pins.
New stages capture the native Slurm job ID, start and deadline before loading
the environment. Their immutable manifest also binds the allocated walltime.
This gives the adapter and scientific runner one allocation-wide deadline without
increasing the method allowance. A native TIMEOUT remains a native failure even
when a retained scientific receipt says completed; it is not an accuracy verdict.
