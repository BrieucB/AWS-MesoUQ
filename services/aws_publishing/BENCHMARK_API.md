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
