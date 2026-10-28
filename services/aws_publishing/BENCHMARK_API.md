# Benchmark Lab control API

Benchmark Lab adds private campaign control to the existing authenticated Lambda.
FastAPI validates typed HTTP requests; Mangum adapts the ASGI application.
The existing API Gateway JWT issuer, client and access-token checks run first.
The cloud service imports no scientific model code. Karolina performs all
preparation, model training, inference, numerical reference and evaluation.

## Owner and operator boundaries

Only an invited project member can create a campaign. Campaigns, stage events,
outputs and archival receipts are private to the creating subject.
An operator must enroll a project `benchmark_policy` with an immutable
`adapter_sha256`, 40-character `science_revision`, and approved
`scenario_families`. Optional `archive_root` pins the durable Karolina archive
destination. Optional `max_concurrent_stages` limits outstanding stage claims;
it is separate from native Slurm association limits and method budgets.

Connector claims and every stage event require an Ed25519 signature from an
operator-enrolled device key belonging to the owner. Signatures bind the
configuration, approved adapter, stage identity and attempt. User requests
cannot select executable paths, checkpoints, arbitrary Python arguments, or
scientific code revisions.

## HTTP routes

- `GET /v1/projects/{project}/benchmarks/capabilities`: server defaults, limits,
  scenario families and supported actions.
- `POST /v1/projects/{project}/benchmarks`: validated, idempotent creation.
- `GET /v1/projects/{project}/benchmarks`: caller-owned campaigns.
- `GET /v1/projects/{project}/benchmarks/{campaign}`: campaign and stage status.
- `POST .../{campaign}/cancel`: durable cancellation intent.
- `GET .../{campaign}/results`: signed, server-accounted comparison.
- `GET .../{campaign}/artifacts/{artifact}`: owner-only presigned download.
- `POST .../{campaign}/archive`: idempotent durable archive request.
- `POST /v1/benchmark-connector/claim`: signed stage claim or reconciliation.
- `POST /v1/benchmark-connector/campaigns/{campaign}/stages/{stage}/events`:
  signed state/accounting events.
- `POST .../stages/{stage}/artifacts`: signed private upload reservation.
- `POST /v1/benchmark-connector/archive`: signed durable archive verification.

Operator deployment fallback policies may be supplied through the trusted Lambda `BENCHMARK_POLICIES` JSON environment mapping. Existing project-state policy takes precedence whenever that field exists; user request fields cannot select source pins or policies. An absent fallback preserves the existing enrollment denial.

Request JSON schemas live in `schemas/aws_publishing/benchmark-*.schema.json`.
The portable client and canonical signing envelopes are in
`aws_mesouq.benchmark_client` and `aws_mesouq.benchmark`.

## Scientific stage and budget semantics

The shared frozen cases precede seven fresh factors:
`global`, `mechanical_1` through `mechanical_3`, and `acoustic_1` through
`acoustic_3`. HNPE inference requires every factor to succeed.
TMCMC and reference use the identical immutable case-set hash. Metrics joins
terminal method outcomes, so a completed comparison can honestly contain
failed or blocked HNPE stages.

Each method has its own fixed total allocated GPU-seconds ceiling.
The metadata CAS transaction reserves GPU time before a stage can be submitted:
cases reserves at most 300 seconds, each factor at most its training budget,
HNPE inference at most 1800 seconds, and TMCMC its remaining method budget.
The cases allocation is conservatively charged to HNPE preparation because the
approved project uses GPU nodes even when the preparation mathematics uses CPU.
Reference and metrics allocations remain separate from either method budget.
There is no global GPU-hour quota in the benchmark API.

An expired `submitting` lease becomes `submission_unknown`. Recovery preserves
the attempt, native scheduler identity and reservation. It requires reconciliation;
it never authorizes blind resubmission. Unknown executions keep their reservations.
Only signed native terminal Slurm accounting settles a submitted reservation.
Native accounting uses actual elapsed seconds and allocated GPUs/CPUs, including
scheduler overhead. Overruns are recorded truthfully, mark the stage failed, and
stop new admissions for that method. Unused reservations are released.

Reported primary cost includes preparation, all fresh training and inference.
Reference cost is shown separately. Missing accounting is unknown, never free.
Elapsed totals are sums of stage elapsed times, distinct from campaign makespan.

## Artifacts and durable archival

Scientific completions bind private uploaded artifact IDs, SHA256 and size.
The service verifies cloud bytes before accepting completion. Archival never
changes scientific status or resubmits numerical jobs.

An owner archive request remains `archive.status=requested` until an enrolled
campaign connector attests to a checksum-verified copy beneath the pinned durable
Karolina root. The receipt includes every retained cloud artifact and a nonempty
manifest of retained scientific outputs. Accepted receipts are immutable and
retries preserve the original verification time.

Benchmark artifacts are retained indefinitely by the current service. Existing
indentation archive maintenance does not delete benchmark objects. An accepted
benchmark receipt explicitly records `automatic_expiry=false`; no 30-day expiry
is promised until benchmark-aware maintenance exists.


## Own-account executor

The separately installed `aws-mesouq-benchmark` CLI uses the existing normal
connector journal. It requires its previously enrolled `connector_id` and
`archive_device`; it never generates replacement device keys. An existing
owner binding must match `GET /v1/me`. If that binding is absent, only the
authenticated API subject may initialize it. The normal calibration journal
entries remain in their original namespace.

An operator supplies a trusted JSON configuration with:
`repository`, `science_revision`, `adapter_sha256`, `python_bin`,
`environment_script`, `environment_sha256`, separate private `run_root`
and `archive_root`, `expected_user`, `expected_host`, `scheduler`,
`stage_seconds`, and exact HTTPS `allowed_upload_origins`.
The scheduler requires an explicitly approved `qgpu_exp` or `qgpu` profile,
account, QOS, CPU count and memory; every stage requests one GPU.
Reference and metrics have separate finite operator stage limits.

The executor snapshots the approved standalone adapter beside the immutable
request and stage manifest. Native Slurm walltime rounds down to complete
minutes within the reserved budget; the scientific process receives a shorter
window with termination grace. The adapter verifies its own hash, the runtime
script hash, a clean pinned scientific Git revision, the full request hash,
and the native allocation before invoking the scientific CLI. TMCMC uses
`mpirun -np 1` inside the allocation to respect the installed MPI stack.

Claims, submission intents, job IDs, upload reservations and event IDs are
journaled durably. Lost acknowledgments replay their original event IDs.
Unknown submission acknowledgment triggers exact-marker, own-user lookup only.
The executor polls all active stages; it cancels only an exact verified own
native job. Missing or invalid scientific receipts produce explicit failures
with actual scheduler cost, while unknown execution retains its reservation.

Only bounded comparison JSON, score CSV, generated PNG and portable stage
metadata are uploaded. Checkpoints, training banks and other retained products
are copied and independently checksum-verified under the approved durable
archive root before the signed archive receipt is sent. The executor reports
bounded error class names and stage counts without logging credentials.


## Reserved cloud storage

Each benchmark campaign and its project have a one-GiB cloud artifact ceiling,
with at most 100 artifacts per campaign and 100,000,000 bytes per artifact.
Creation atomically reserves the conservative expected output size:
three methods times cases times posterior draws times 21 float64 parameters,
plus eight MiB of overhead. Admissions include retained legacy artifacts and
all benchmark campaigns in the project. Uploads consume the existing expected
reservation without counting those same bytes twice. Larger actual output may
grow the charge only while project and campaign capacity remain available.
Terminal campaigns release unused expected space only after every stage is
terminal; actual object/upload reservations remain charged. Existing v1 owner
limits remain in force.

Scientific request identity normalizes the declared scenario multiplier and
excludes only the transport idempotency key. Full campaign transport identity
retains that key. Integer budgets and scientific reference limits are preserved
across the typed HTTP interface and the scientific CLI.

Metrics receives a pinned allocation-cost snapshot only after every other
stage is terminal. Its native allocated GPU/CPU totals carry explicit unknown
status when accounting is missing. The scientific receipt must bind the same
cost-file checksum; process wall clocks remain diagnostic rather than primary
method cost.

Archival commits a compact cloud receipt to a full immutable internal manifest.
The connector still copies and independently verifies every original file
against that manifest, including model weights and native diagnostics. The
signed original-file count and snapshot hash are connector attestations;
the cloud service cannot inspect PROJECT files independently. Unverified
partial cloud uploads are explicitly quarantined and remain storage-charged;
they are not falsely described as committed archived results.

Every scientific invocation also receives the globally unique cloud campaign
instance ID. Scientific stage dependencies, receipts, checkpoints and allocation
costs must bind that instance separately from the scientific and transport
request hashes. Equal requests in different projects cannot reuse a checkpoint.
This explicit binding remains valid when durable archives are relocated.


Publication records per-object progress but treats it only as a local hint.
A signed, owner-scoped single-object status probe verifies the reserved SHA256
and byte size against private object storage before accepting a prior PUT.
Existing mismatched bytes fail closed and are never overwritten.
Lost acknowledgments and conditional-PUT 412 responses require that same
backend verification. Retries replay the original reservation event and refresh
signed URLs without creating duplicate object IDs or capacity reservations.
Only one artifact body is buffered at a time; checkpoint/training products
remain PROJECT-only. Older journals without the original reservation event ID
can renew the same existing object through the signed status probe.

Benchmark object keys use
`projects/{project}/artifacts/benchmarks/{campaign}/{stage}/{artifact}`,
within the existing private runtime object permission boundary. Their metadata
remains in benchmark campaign records; legacy UUID artifact lifecycle processing
does not enumerate or expire this namespace.

Terminal campaigns that never submitted a native scientific stage expose
`archive_capability.available=false` with reason
`no_executed_scientific_stages`. An archive request returns an explicit
`archive_unavailable` error; it never creates a pending or verified receipt
for scientific outputs that do not exist.
