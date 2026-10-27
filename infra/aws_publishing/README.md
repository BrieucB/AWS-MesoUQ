# AWS-MesoUQ infrastructure source

These Terraform inputs belong to
[BrieucB/AWS-MesoUQ](https://github.com/BrieucB/AWS-MesoUQ). They describe a proposed
private application, not deployed resources. `name` defaults to `aws-mesouq` and
GitHub OIDC defaults to that exact repository. Actual account/resource identifiers,
operator memberships, approved models and keys are supplied privately later.

The module defines private S3 data and website buckets, CloudFront/OAC,
DynamoDB job metadata, encrypted SQS revision notifications/dead letters, ECR,
Lambda, HTTP API Gateway, required-TOTP invitation-only Cognito Lite/classic UI,
CloudWatch logs, a monitoring budget, daily private archive reconciliation and a
restricted GitHub environment OIDC publication role. SQS is not job authority;
DynamoDB is the durable ledger. The service limits admission to ten active jobs;
operator membership configuration allows at most five invited subjects.

The service exposes only one-CSV, one-diameter calibration against the approved
locked preset. Prediction, population/joint calibration, retraining and model
release are unavailable through the production HTTP handler. See
[the service contract](../../services/aws_publishing/README.md) and
[the shared preset](../../src/aws_mesouq/presets/indentation-v1.json). Scientific
weights, banks, curves, results, runtime paths and private MesoUQ code are not
publication inputs. Karolina and Vega source interfaces remain supported; live
Vega validation is deferred.

## Owner prerequisites

Deployment has not been authorized. No workflow may deploy resources, invoke AWS,
invite users or upgrade an account plan as part of offline validation. The approved
budget is €0 out of pocket under the AWS Free account plan. The owner must verify
account-plan/service eligibility, credit expiry and usage coverage before any live
operation. A monitoring budget is not a spending cap; the module does not create
or upgrade a pricing subscription. CloudFront Free flat-rate plans have distinct
feature restrictions; do not silently substitute or upgrade plans.

Keep Terraform state, plans, credentials, tfvars and signing private keys outside
the public repository. State contains identity and model metadata. Establish an
owner-controlled secure state backend/access policy before shared operation; this
module creates no implicit state backend. Its `.gitignore` excludes local state,
plans and private tfvars, and is not a substitute for controlling state access.

Initial ECR/image bootstrap requires separate owner-reviewed steps: create ECR,
build/push the lightweight service image, then supply its immutable `@sha256:...`
reference for a full plan. Never apply a placeholder image. The Dockerfile copies
only dependency-light contracts/release/preset plus the control plane, excluding
connector/scientific code. An existing GitHub OIDC provider ARN is required; the
publish role trusts only the exact repository/environment and allows ECR pushes,
website upload and CloudFront invalidation. It cannot update Lambda or administer
IAM, Terraform, Cognito, DynamoDB or the science data bucket.

Generate an RSA2048 key pair using an approved owner workflow. Supply only the
public PEM to `site_signing_public_key_pem`. Separately provision its matching
private PKCS8 PEM in SSM **Standard SecureString**, encrypted with the AWS-managed
`aws/ssm` key; supply the parameter name only. Never generate/read the private value
through Terraform or place it in state, plans, edge code, browser assets or source.
Lambda can read only that parameter and caches the key regionally. Rotation requires
coordinated public/private replacement and Lambda cold starts; allow five-minute
sessions to expire or remove the old public key to revoke immediately.

Cognito users are administrator invitations with interactive password change,
TOTP enrolment/recovery and verified-subject lookup. Map those subjects to approved
projects/roles privately; browser claims cannot create memberships. The default
connector callback `http://localhost:8765/callback` is an exact testing-only
loopback callback. Production HTTPS authentication placement remains an owner gate;
change the connector and registered callback together. Quiesce the API before
Terraform membership/model item changes: direct operator writes do not participate
in the running service revision transaction. Runtime revocation needs an audited
transactional operator process or maintenance window.

Only operator-trusted released immutable models may be bootstrapped. For release
v1, `provenance_json` must encode portable canonical provenance including the
`uq_emb_sonovue_202607` pipeline binding. Never copy private installation metadata,
scientific profile, training-support paths or artifact roots. Per-diameter model
SHA256 maps must cover selected diameters. The API pins that descriptor into each
job and rejects canonical requests lacking matching binding. No browser registration
or runtime model release exists in v1. Install approved model bytes and matching
private scientific profile in each own-account HPC connector separately.

## Website and API privacy

`website_url` points to `/auth/login`. CloudFront requires signed cookies for every
HTML/JS/CSS/configuration asset; direct unauthenticated `/` or asset requests are
denied. Both S3 buckets deny public access and non-TLS requests. Only `/auth/*`
targets the separate public regional OAuth bootstrap, without CORS; unknown auth
paths return JSON 404 and never assets. API requests retain Cognito JWT and current
invitation/project/owner checks.

OAuth state/nonce/PKCE and fixed redirects protect callback. Regional RSA signing
issues signed cookies and encrypted HttpOnly host-only session cookies with an
absolute five-minute maximum bounded by token expiry. Session cookies have Secure,
SameSite=Lax, Path=/ and no Domain; their `__Host-` prefix applies to encrypted
session/OAuth cookies. No refresh token is retained. GET `/auth/session` rechecks
signature, token identity and current invitation, and hydrates an in-memory bearer
token with no-store/private headers. POST `/auth/logout` requires the exact site
Origin. API calls check membership immediately; copied asset cookies can retain
access for at most five minutes. Downloaded assets cannot be recalled.

Security headers include CSP, HSTS, frame denial, no-referrer and nosniff. Auth
responses never cache; cookie/query/Origin forwarding serves OAuth only. No access
logging captures query strings, cookies, codes or tokens. Private key material
remains regional; this design has no Lambda@Edge.

## Retention and live acceptance

Verified science objects remain indefinitely without a valid verified Karolina receipt.
The server durably records the first verified receipt time before permitting tags.
Only after thirty days from that server time can reconciliation apply
`archive_verified=true`; the existing creation-age S3 rule then expires eligible
objects. Later receipts cannot shorten or restart retention. Daily reconciliation
and S3 processing may extend it. Uploads cannot add that tag inline. The private
EventBridge rule is IAM scoped to its exact ARN/account, and handler envelope checks
reject HTTP attempts to select maintenance. Failed tags retry with bounded batches,
SDK timeouts and successful-prefix checkpoints. DynamoDB jobs, summaries and receipts
have no TTL and remain after S3 expiry. AWS receipt checks do not independently
verify HPC backup durability.

Incomplete multipart uploads abort after one day; that is separate from verified
artifact retention. Buckets are not force-destroyed and Cognito/DynamoDB deletion
protection is enabled. CloudWatch retains seven days, SQS notifications one day and
dead letters four days; ECR retains three untagged images. No VPC/NAT or idle HPC
compute cluster is created. Account quotas may reject reserved Lambda concurrency;
resolve that with owner review without silently changing the approved budget/plan.

Offline commands, when Terraform and the locked provider are locally available:

```sh
terraform -chdir=infra/aws_publishing fmt -check -recursive
terraform -chdir=infra/aws_publishing init -backend=false
terraform -chdir=infra/aws_publishing validate
```

Live acceptance remains owner-gated: invite/TOTP/PKCE and revocation, entire-site
CloudFront denial/access, CSP/browser loading, immutable upload/checksum behavior,
competing DynamoDB admission/lease recovery, SQS observability, archive/tag/expiry,
verified durable Karolina storage and credit/export procedures. Production-size
scientific validation requires separately approved HPC runtime. Source tests and
offline Terraform checks establish none of these live properties.

## Device enrollment, approved adapter and upload limits

Each operator-managed membership can include `connector_public_keys`, a map from
connector ID to base64 raw 32-byte Ed25519 public key. Enroll only an independently
verified public key from the private connector journal; private keys never enter
Terraform or AWS. There is no HTTP enrollment route. Succeeded events and archive
receipts require separate signed canonical envelopes bound to job/project/owner/
connector/event identities; success also binds the recorded Slurm ID and exact
result. Archive eligibility additionally requires a succeeded recorded Karolina
job and an archive-capable lease. A browser JWT cannot forge that authority.

`trusted_models.approved_adapter_sha256` is required. Freeze/review the public
adapter source, calculate its SHA256 and supply the approved value privately before
bootstrap. The API snapshots the pin; connector staging and completion compare it.
A metadata pin and signature identify approved execution authority, not independent
scientific validation. Complete verified publication bundles and exact uploaded
result/provenance/settings JSON are required before v1 success.

Upload reservations enforce 1 MiB curves, 25 MiB outputs, 256 MiB per user, 1 GiB
project/global total and 1000 retained artifact reservations. Pending uploads count
immediately, and presigned PUTs bind Content-Length. The private daily maintenance
first commits expiry for unverified uploads older than 24 hours, blocking completion,
then deletes their server-owned objects and releases quota. Scoped DeleteObject
permission covers only `projects/*/artifacts/*`. Failed deletions retain reservations
and retry without deleting verified files. Verified uploads, even unused ones, remain
until trusted archive retention matures; quota releases only after S3 confirms actual
expiry/absence. DynamoDB history remains. The operator resolves maintenance errors
and allows the existing private schedule to retry; no browser cleanup route or
blanket verified-data deletion is provided.

Release v1 accepts 2–1000 finite decimal numeric rows per mechanical CSV, validated
from the actual checksum-bound bytes before upload verification. Headers are optional;
quoted/nonfinite/nondecimal data fails. Dense inputs are rejected intact rather than
decimated. Fitted arrays also have a 1000-point cap, preserving the current HTTP and
DynamoDB summary bounds without silently changing scientific data.
