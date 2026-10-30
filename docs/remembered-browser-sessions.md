# Remembered browser sessions

A browser can explicitly choose **Keep me signed in on this browser** on the public
sign-in form. Its original session ends after at most 30 days or after seven days
without foreground activity, whichever comes first. An unchecked form keeps the
legacy five-minute site session. The setting is bound into the encrypted OAuth
state; a query parameter cannot silently opt a user in. MFA, PKCE, state, nonce,
issuer, client and current invitation checks remain in force.

The existing Cognito app client remains unchanged, including its native connector
callback and one-day refresh lifetime. A separate public remembered-web client
uses the exact website callback, 15-minute access/ID tokens, 30-day refresh tokens
and token revocation. Its direct API flow permits refresh only; sign-in uses the
hosted authorization-code/PKCE path. It creates no client secret. API Gateway and the backend pin
the old and remembered client IDs. The existing frontend runtime client ID remains
valid for the fixed Cognito logout redirect.

## Browser contract

GET /auth/login first resumes a still-valid remembered browser and renews its short
asset grant before redirecting to the protected app. This cold-entry path does not
extend idle expiry. Otherwise it serves a small public, no-script form with an
unchecked checkbox. The form POST requires the exact site Origin, its short-lived
encrypted intent cookie and the matching hidden CSRF value. Refresh credentials
cannot recover an already-expired pre-feature browser session; one fresh sign-in
is required when adopting the feature.

GET /auth/session retains the legacy response. Remembered responses contain:

- access_token: bearer token for browser memory only;
- expires_at: short site/edge-grant epoch, at most five minutes;
- renewable: true;
- absolute_expires_at and idle_expires_at: server-enforced epochs;
- generation: integer server record version, not a credential.

POST /auth/refresh requires the exact Origin, application/json and exactly
{"activity": boolean}. Foreground interaction can set activity=true. Timers,
status polling and tab-resume checks use false; they never extend the seven-day
idle end. The original 30-day end never moves. A successful remembered refresh
returns the fields above and renews the opaque HttpOnly cookie and short signed
CloudFront cookies. A valid legacy cookie returns its existing, unextended
five-minute session.

A missing, revoked or expired remembered session returns 401. A GET bootstrap
whose short grant needs renewal returns 401 with error.code=refresh_required; the
frontend can make one refresh(false) attempt before requesting sign-in. Invitation
or Origin denial returns 403. Concurrent renewal returns 409, code
refresh_in_progress and Retry-After: 1. Transient provider errors return 503 and
must not be interpreted as a fresh calculation submission or an automatic MFA
requirement. Malformed refresh JSON returns 400. Responses never cache or enable
cross-origin token hydration.

## Credential custody and concurrency

The browser receives a random opaque __Host-mesouq-remembered cookie with Secure,
HttpOnly, SameSite=Lax, Path=/ and finite Max-Age; no Domain is set. Refresh tokens
never enter JavaScript, browser storage, cookies, URLs, Terraform, source or logs.
The regional service encrypts the complete credential/session payload with a
domain-separated key derived from the existing SSM-held signing material.

Records use pk AUTH_SESSION#<SHA256 of opaque SID>, sk SESSION in the existing
DynamoDB table. They are outside STATE and its BUCKETS, revision, admissions and
metadata snapshots. Point reads and version-conditional writes implement one
short refresh lease per SID. The original family end and idle end are checked
before activity can extend the latter. A CAS logout tombstone wins over an
in-flight provider response. Refresh keeps the same SID, so a late Set-Cookie
cannot create a new active family after logout. Session records use
auth_expires_at for eventual TTL cleanup only; explicit expiry checks are always
authoritative.

Current invitation checks use a consistent point read of the existing identity
record. The auth and business routes still share the existing Lambda and role.
This is code/record isolation, not route-level IAM isolation. Existing business
IAM is preserved; its existing table GetItem/PutItem permissions already support
session CAS, so no new runtime IAM action is required.

Logout tombstones the local family, best-effort revokes its provider refresh
credential, clears all browser/auth/edge cookies and redirects through the
existing Cognito logout endpoint. A provider outage cannot undo a committed local
tombstone. Frontend generation guards and cross-tab logout notifications discard
late token responses.

Already-issued access JWTs remain subject to the existing offline JWT validation
window (at most 15 minutes); copied signed asset cookies can remain usable for up
to five minutes. Removing the invitation blocks future application API calls
immediately. This feature does not promise instantaneous global JWT invalidation
or recall of downloaded data.

## Reviewed activation sequence

Source implementation does not authorize any AWS or security change.

1. Review and test backend, frontend, full API/storage compatibility and the
   existing native connector. Keep the running Benchmark Lab release separate.
2. Obtain owner approval for the exact security/resource/deployment changes,
   including the additional CloudFront Function and viewer association.
3. Prepare the dedicated remembered-web public client, existing-table
   auth_expires_at TTL and the authorizer's two exact audiences. Preserve the old
   app client, required MFA, Lambda role, callbacks, budgets and scientific pins.
4. Publish the reviewed immutable backend image with the feature disabled.
   The Terraform remembered_browser_sessions_enabled switch defaults to false;
   the dedicated client ID remains configured so logout can revoke existing
   families during a rollback while issuance and renewal remain disabled.
   The existing function-only publication workflow does not acquire Cognito,
   API-authorizer, DynamoDB configuration or CloudFront Function/distribution
   mutation permissions.
5. Capture only nonsecret resource metadata and run
   tools/prepare_browser_session_release.py --metadata METADATA.json --output
   PRIVATE_ACTIVATION.json on the approved execution host. It validates the
   unchanged original client, MFA, web-only new client, token lifetimes, both
   audiences, TTL, Lambda revision/environment before-image, exact published
   viewer-function code SHA/ETag and distribution ETag. Its distribution candidate
   permits only the reviewed viewer-request association change. It makes no AWS
   calls and has no apply mode; its output is owner-only and never overwrites.
6. After action-time approval, activate only SITE_REMEMBERED_SESSIONS_ENABLED using
   the recorded revision/environment guards (or the reviewed Terraform switch).
   Preserve every other environment value, especially the already provisioned
   COGNITO_REMEMBERED_CLIENT_ID and benchmark policies. Read
   back the exact configuration and source/image identity.
7. Test the reviewed viewer association after approved activation before
   declaring cold root/index reload supported. Keep the public /auth/login entry
   available as the safe fallback. Use the
   [remembered website publication profile](remembered-website-publication.md) to
   publish the matching frontend, then perform one explicit remembered sign-in and
   verify renewal beyond five and fifteen minutes, cold entry, form/draft
   preservation, idle/absolute boundaries, invitation denial and cross-tab logout.

The resource changes reuse Cognito, Lambda, API Gateway, CloudFront, SSM and
DynamoDB. No EC2, RDS, paid Cognito tier, new Lambda function, client secret,
connector credential, journal/key migration or HPC calculation is introduced.

## Conditional root/index entry routing

A small CloudFront Function on the existing distribution is disabled by default
with the activation flag. For GET or HEAD of exactly / or /index.html, it returns
a fixed no-store redirect to /auth/login if any of CloudFront-Policy,
CloudFront-Signature or CloudFront-Key-Pair-Id is missing or empty. It neither
parses tokens nor logs, calls a service, reads an opaque SID or serves HTML.
All other paths and requests with populated grant cookies pass through unchanged;
CloudFront retains signature/expiry validation and the original trusted key
groups. Assets and configuration never become public.

Remembered-mode edge cookies have an explicit finite Max-Age equal to their
remaining signed grant (at most 300 seconds). Legacy issuance attributes remain
unchanged. Synthetic tests verify the finite attributes, exact routing scope and
fall-through for invalid populated signatures.

AWS documents viewer-request Functions as running before further processing, but
does not explicitly name their ordering relative to native signed-cookie
rejection. The proposed cold-root behavior is therefore an inference until live
acceptance. The offline release record marks ordering_unverified_until_live=true.
After action-time approval, an isolated browser must request root/index with
missing/expired remembered grants, prove the redirect and valid-SID resume without
MFA, and confirm unsigned private assets still fail. If ordering differs, native
403 remains intact; no authentication bypass is introduced and the fallback is
/auth/login. Do not declare cold reload complete from synthetic tests alone.
