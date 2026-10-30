# AWS-MesoUQ private control plane

This Python 3.11 service in [BrieucB/AWS-MesoUQ](https://github.com/BrieucB/AWS-MesoUQ)
contains the cloud control plane. Scientific execution, approved checkpoints,
polynomial banks and reference data remain in the private MesoUQ installation on
HPC. The Lambda image copies only `aws_mesouq` contracts, release policy and preset,
plus the service and its HTTP/auth dependencies. It excludes the connector and
scientific runtime.

## Release v1

Only calibration of one CSV mechanical curve at one nominal diameter (3.2, 3.4 or
5.8 µm) is admitted. The authoritative settings are
[`indentation-v1.json`](../../src/aws_mesouq/presets/indentation-v1.json).
`aws_mesouq.release.configuration(diameter)` clones those settings and changes only
`emb_diameters`. Admission normalizes the configuration with the shared validator
and requires equality with that approved preset. Population/joint calibration,
prediction, training, sampler edits, and model release are unavailable in v1.
Successful results must retain the approved 0.9 interval probability.

`Application` defaults to v1. The production HTTP handler explicitly constructs v1,
independently of any injected general validator. No body, token or environment
setting can relax this policy. `release_policy="nonproduction_legacy"` exists only
as an explicit internal constructor option so lifecycle/security regression tests
can exercise historical contracts. It is never selected by the HTTP handler.
Model listing remains available for selecting an operator-approved released model;
`POST /v1/projects/{project}/models/{version}/release` returns 404. Uploads of
training tables, model and validation artifacts are rejected. Calibration output
artifacts (result, posterior, log, manifest) remain supported.

Curve references must identify exactly one verified `text/csv` artifact named with
a `.csv` suffix, explicit force/displacement units, column order and matching
nominal diameter. Completion and admission verify stored checksums and sizes; HPC
validates CSV scientific contents. Filenames never select storage paths or code.

## Authentication and job authority

API Gateway verifies Cognito JWTs; the handler additionally checks issuer, client
ID and access-token use. Invitation records in DynamoDB determine membership and
roles. Request bodies and JWT groups cannot grant access. Provision at most five
invited users through the operator-controlled flow and require TOTP MFA. There is
no membership-management or signup HTTP endpoint. Disabling the invitation denies
future API calls even before token expiry.

CloudFront signed cookies protect every website asset. Only `/auth/login`,
`/auth/callback`, GET `/auth/session`, POST `/auth/login`, POST `/auth/refresh` and POST `/auth/logout` form the public OAuth
bootstrap; unknown auth routes return JSON 404 and never assets. State/nonce/PKCE,
Cognito signature/issuer/client/token-use checks, current invitation checks and
fixed redirect destinations protect login. The regional signer reads its private
RSA2048 PKCS8 PEM from separately provisioned SSM Standard SecureString; Terraform
and the repository contain no private key. Unchecked legacy sign-in keeps its
five-minute encrypted session. Explicit remembered-browser sign-in uses an opaque
HttpOnly cookie and encrypted server records with a 30-day maximum/seven-day idle
end; signed asset grants remain at most five minutes. The portal keeps hydrated
access tokens in memory. See [Remembered browser sessions](../../docs/remembered-browser-sessions.md). Session hydration has no CORS and
rechecks membership; logout requires the exact Origin. API calls always check
current membership. Asset-cookie revocation may take up to five minutes.

DynamoDB is the durable job authority. SQS carries encrypted, best-effort revision
notifications; duplicate/lost notifications cannot submit or lose jobs. Consistent
snapshots and conditional revision transactions serialize ten-active-job admission,
request idempotency and event replay. The small-user implementation reads retained
metadata snapshots; it requires redesign before scaling beyond the approved scope.
Metadata has a 300 KB record guard and requests a 200 KB body guard.

Claims/events require the job owner, project, connector identity and valid lease.
Site identity remains Karolina or Vega. Expired leases preserve scheduler identity;
`submitting` becomes `submission_unknown` and must be reconciled locally before
submission. Cancellation is a recorded request executed by the own-account HPC
connector. Event replays remain idempotent across lease rotation; modified replay
content fails. `exclude_job_ids` accepts at most ten unique bounded IDs for fair
sequential sweeps without changing authorization.

Every admitted job snapshots its immutable approved model hashes, portable pipeline
binding and available validation. Canonical requests require the authoritative
`uq_emb_sonovue_202607` registry binding. Success must match exact job, model, input,
normalized configuration hashes and the pinned pipeline binding. Private model
installation/support paths stay on HPC. Operator-reviewed registry installation is
required in each connector account; browser uploads cannot register models.

## Uploads and archive retention

Server-generated object keys and signed PUT headers bind content type, SHA256 and
`If-None-Match: *`. Uploads expire after fifteen minutes and authenticated downloads
after five. Verified objects remain indefinitely until the authenticated owner connector
supplies a complete Karolina receipt covering verified input/output checksums and
sizes. AWS checks receipt consistency, not independent filesystem durability.

The first accepted receipt durably records server verification time. Objects remain
for at least thirty days after that time, regardless of upload age; later receipts
cannot shorten or restart it. A private daily EventBridge invocation and bounded
request reconciliation apply the lifecycle tag only after that deadline. Short
single-attempt SDK timeouts and time-budget checks permit checkpointing a successful
prefix. Failed tags retry; reconciliation/S3 processing can extend retention.
Scheduled invocation validates the exact rule/account/region envelope; there is no
HTTP maintenance route. DynamoDB summaries, jobs and receipts have no TTL and
remain after S3 copies expire.

## Local validation and deployment

```sh
PYTHONPATH=src:services/aws_publishing /usr/bin/python3.11 -m pytest tests/test_release.py services/aws_publishing/tests -q
PYTHONPATH=src:services/aws_publishing /usr/bin/python3.11 -m aws_publishing.local
docker build -f services/aws_publishing/Dockerfile .
```

The local server binds only 127.0.0.1:8765 with explicit in-memory identity and
metadata. Its `memory://` upload URLs are test doubles. Never expose that server.
Offline tests cover both Karolina and Vega interfaces; live Vega validation is
deferred. No live AWS or scientific execution is established by source tests.

See [infrastructure instructions](../../infra/aws_publishing/README.md). The project
has no deployed AWS resources. ECR bootstrap, owner-controlled signing keys/OIDC,
account eligibility/credits, invitations and live acceptance remain operator gates.
No deployment, publication, AWS plan upgrade or paid operation is authorized here.

## Enrolled connector attestations and complete results

Release v1 requires an Ed25519 device key registered by the operator in the job
owner's invitation record: `connector_public_keys` maps connector IDs to base64
raw 32-byte public keys. The private key stays in the connector's private journal;
there is no browser enrollment route. Cognito bearer tokens alone cannot assert
scientific success or archive eligibility. Operator changes require the same
quiesced/CAS-aware process as membership changes.

A succeeded event requires `result_signature` over canonical compact sorted JSON
with `version: 1`, job/project/owner/connector/event identities, `type: succeeded`,
`slurm_job_id` and the exact result. An archive event requires `archive_signature`
over the same identities, `type: archive_verified` and exact archive receipt.
Only the lease token and signature are excluded; other unsigned event fields are
rejected. Both signatures use raw 64-byte Ed25519 signatures encoded as base64.
Lease rotation preserves safe event replay without allowing altered content.

Archive acceptance additionally requires a succeeded job whose recorded site is
Karolina and whose current lease declared `archive_capable: true`. Failed,
cancelled, Vega and ordinary leases cannot begin retention clocks. The path must
be canonical and absolute; neither its spelling nor a signature independently
proves filesystem durability. The signature identifies the enrolled connector that
verified the private copy. Shared same-project inputs can be reused, but only this
trusted attestation can start their first archive clock.

V1 model registry records must pin `approved_adapter_sha256`, copied immutably into
the job. Completion requires that exact adapter fingerprint, exact pipeline binding,
finite ka/kb/d0 intervals with their DPD units, one fitted curve, 0.9 probability and
conditional fixed-kb resonance assumptions or an explicit unavailability reason.
Every declared publication file must resolve one-to-one to a verified owner-created
artifact with exact name, kind, media type, length and checksum. Required downloads
are one posterior CSV, parameter intervals/fitted curve/resonance CSVs,
result/provenance/settings JSON, and PNG plus PDF figures for posterior, fitted
curve and resonance. The stored result JSON must equal the event result excluding
its subsequently appended artifact references; stored provenance and settings are
also compared exactly. Signing identifies approved execution authority and does not
independently establish the mathematical truth of a result.

## Upload capacity and reclamation

The server reserves capacity before issuing an upload URL, counting pending and
verified objects together: curves are limited to 1 MiB per file, outputs to 25 MiB,
each user's retained reservations to 256 MiB, project/global reservations to 1 GiB,
and retained artifact count to 1000. Limits reject further uploads without changing
scientific rows or truncating data. Presigned PUTs bind Content-Length as well as
checksum/content type/immutability. Browsers automatically supply Content-Length
from the selected file; clients must send exactly the declared byte count.

The existing private daily maintenance invocation reclaims old unverified uploads:
after 24 hours it commits `upload_expired_at` with the global conditional revision
before deleting the object, then commits `reservation_released` only after deletion
succeeds. Completion rejects expired records. If completion wins the revision race,
cleanup reloads and leaves the verified object untouched. Failed deletes retain the
reservation and retry on maintenance; batches and remaining-time checks bound work.
No verified scientific object is deleted by this cleanup, including unused uploads.
For mature archive-tagged verified objects, quota is released only after S3 confirms
the object is absent. Corrupted-but-present objects retain their reservation. All
DynamoDB summaries remain. Maintenance/retry failures can delay reclamation and
cause uploads to return a quota error until the operator resolves the failure and
the existing private schedule retries; there is no public cleanup endpoint.

CSV completion validates the actual checksum-bound bytes before setting `verified`:
2–1000 data rows, two unquoted finite decimal/scientific-notation columns, an optional
text header, UTF-8 BOM and blank lines. A 1001-row upload is rejected intact; no
samples are decimated or silently truncated. Fitted result curves are also capped
at 1000 points so their complete normalized arrays fit the existing HTTP/DynamoDB
summary envelope. The 1 MiB file cap remains an independent byte limit.
