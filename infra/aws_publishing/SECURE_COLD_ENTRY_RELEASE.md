# Secure cold-entry release (staged, no broad Terraform apply)

This change separates the public bootstrap from the signed application document.
AWS documents that a viewer function URI rewrite retains the selected cache
behavior and origin: [CloudFront Functions request fields](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/functions-event-structure.html).
The signed default behavior rewrites only exact GET/HEAD /app to /index.html.
It does not authorize a request. CloudFront trusted key groups remain authoritative.

Create the new private-entry-v1 and public-entry-v1 function identities and publish
their reviewed code while they are unassociated. Never update attached function
code in place. Terraform's new flags default false and describe the final state;
a broad apply is not a release mechanism. Retain the old remembered-entry hook.

1. Freeze the integrated reviewed source commit and green allocated/CI evidence.
   Capture the complete reviewed initial DistributionConfig (canonical SHA256
   16f33e77424ec38c75f77456033dc133dd77a57f9bed1ee8f7d02c207cb40ad3).
   The planner accepts no replacement baseline or distribution.
2. Capture fresh live metadata and prepare private-alias. Review its exact CAS
   request: only DefaultCacheBehavior.FunctionAssociations may change. Wait for
   Deployed; prove authorized GET/HEAD /app succeeds and unsigned GET/HEAD /app
   remains denied. Check normalized variants and repeat after warming the cache.
3. Publish the separately reviewed application backend redirect and one-file
   static profile. Confirm /app bootstrap, /v1/me authorization, history URL
   cleanup, legacy SESSION bounds, remembered family renewal, and no extra MFA
   for the valid remembered owner. Preserve all science/client/key identities.
4. Capture fresh live metadata and actual API routes. Prepare public-entry only
   after the application/native alias proof exists. Its one CAS clears
   DefaultRootObject and inserts exact / and /index.html behaviors before the
   unchanged auth behavior. Their only fallback origin is regional-auth; they
   forward no viewer cookies, query strings or Authorization. The function
   unconditionally returns 302 /auth/login with no-store. If it is unavailable,
   the actual API has only ANY /auth/{proxy+}, so both paths return a public404.
5. Wait for Deployed and run native public cold entry plus private denial checks.
   Check GET/HEAD root/index with missing and expired grants, HTTP-to-HTTPS,
   root/index normalization variants, no loops, security headers, warm-cache
   denial of /app, index-backed application content, JS and runtime config.
   Source mocks cannot establish CloudFront signature/function execution order.

## Metadata input

The planner never calls AWS, obtains credentials or applies a change. Run with
/usr/bin/python3.11 tools/prepare_cold_entry_release.py --metadata INPUT --output OUTPUT.
The output must be a new canonical-path file; it is created mode0600. Do not put
tokens, cookies, session rows, private keys or credential data in either file.

The strict top-level schema is aws_mesouq.secure_cold_entry_metadata.v1:
- reviewed_source_commit: exact clean checked-out integrated commit. The planner
  compares its own source, functions, publisher and workflow with git show.
- static_baseline_commit: dace4a501cb539d61ecad42e592f36b8666f2cc1, an ancestor.
- operation: private-alias, public-entry, rollback-public or rollback-alias.
- distribution: exact projection Id, ARN, ETag, Status, DistributionConfig from
  fresh GetDistribution. Status must be Deployed and the image must match the
  selected stage. The full DistributionConfig is preserved, including all
  fields/order outside each permitted delta. Unknown initial config bytes are
  rejected by the reviewed canonical hash.
- initial_distribution_config: exact already-reviewed full initial config.
- functions: legacy/private/public, each an exact projection ARN, ETag, Stage,
  Runtime, Code of the LIVE function metadata/code responses. All must be in the
  distribution account and match the source bytes and versioned identities.
- auth_api: ApiId, ApiEndpoint, ProtocolType, Routes. Routes is the actual complete
  GetRoutes Items projected to RouteId, RouteKey, Target, AuthorizationType.
  Only one route ANY /auth/{proxy+} is accepted; no default/root/index route.
  No source-only assertion may substitute for the live GetRoutes response.
- cache_policy: Id, CachePolicyConfig from GetCachePolicy. The existing policy
  must have zero TTL and none for cookie/header/query forwarding.
- s3: Bucket, PublicAccessBlockConfiguration, parsed Policy, OriginAccessControl.
  OriginAccessControl has Id and OriginAccessControlConfig from live metadata.
  Only the existing distribution-scoped CloudFront GetObject allow and TLS deny
  are accepted, with all four public-access blocks and always/SigV4 S3 OAC.
  No ListBucket allow, new principal, public policy, key values or IAM changes.
- application_proof: null except for public-entry. For that stage use exact
  source_commit, backend_revision_id, backend_image_sha256, app_js_sha256,
  report_sha256 and alias_config_sha256. The reviewed report must contain the
  native positive/negative alias proof, deployed image/revision/static hash and
  application checks; this is an evidence reference, not a substitute for review.

Resource metadata snapshots are nonsecret projections, never handmade claims.
Retain their private evidence hashes with the owner-approved plan and requests.
Recapture ETags immediately before an approved mutation; AWS CAS remains the
authority. Any mismatch requires fresh diagnosis/review, never a force update.

## Static single-file profile

The dedicated publish-private-entry-website.yml workflow is manual/main-only,
uses the existing protected environment and OIDC role, and serializes on the
existing website publication lock. Its session policy grants GetObject to
exactly14 existing keys and PutObject only to src/app.js. There is no ListBucket,
wildcard resource, delete, new service or IAM expansion.

Before credentials, actual Node22 target and fixed dace baseline builds must
contain exactly14 files and differ only in src/app.js. The old remembered-session
profile must reject that pair. The new profile verifies14 objects before any
write, performs one If-Match write (or zero for an exact idempotent rerun), then
reads back all14. It preserves the other13 live bytes and ETags, including private
runtime-config bytes; the build's runtime config must also be unchanged. This is
14 allowed read keys, with28 successful object downloads for full pre/readback.

## Guarded rollback

Reverse C first. Capture the current Deployed config and fresh ETag, set operation
rollback-public and application_proof null, then prepare a new reverse CAS. It
accepts only the exact generated public image and restores the alias image,
removing only the two public behaviors and restoring DefaultRootObject=index.html.

Only after that reversal may the separately recorded backend image be restored.
Leave the harmless history normalizer and alias until redirects return to /.
Then rollback-alias can restore only the original default function association.

Forward output includes candidate hash and reverse-operation guard, not a stale
fabricated rollback IfMatch. A reverse request is produced only from fresh live
metadata. Never delete functions, assets, Cognito clients, session families or
keys. No broad Terraform, auth environment rewrite, model or connector change.

## Allocated QA handoff

Use one CPU and at least1GiB, max5minutes for focused offline tests (Node22 and
/usr/bin/python3.11). Run only inside a parent-owned allocation, never a login
node. Run all existing publication/workflow/entry/session-planner regression
tests plus test_cold_entry_release.py, test_secure_entry_functions.py and
test_private_entry_website_publication.py. Build actual target and dace baseline
with npm run build, supply AWS_MESOUQ_WEBSITE_CURRENT_DIST and
AWS_MESOUQ_WEBSITE_BASELINE_DIST, and run PrivateEntryBuildTests explicitly.
Run npm test for the integrated frontend. CI and independent review must use
the final integrated frozen commit. No claim of native proof or recovery follows
from these source-level tests.
