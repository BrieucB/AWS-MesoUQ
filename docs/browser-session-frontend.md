# Browser session renewal

Remembering a browser is an explicit, unchecked-by-default choice on the server's
public sign-in form. The server binds that choice to its short-lived OAuth state.
The existing application Sign in button still opens /auth/login. JavaScript does
not make the choice, receive an opaque SID, or receive or store a refresh token.

The app receives only a short-lived access token in memory. A remembered response
also contains renewable, absolute_expires_at, idle_expires_at and generation.
The maximum remembered lifetime is 30 days, with a seven-day idle limit; the BFF
enforces both. Short site/edge grants remain at most five minutes, and issued API
JWTs retain their configured maximum fifteen-minute validity. Logout revokes the
server SID; already-issued grants have those bounded residual lifetimes.

## Renewal and activity

SiteSession keeps one in-flight renewal per tab. It renews about four minutes
into a normal five-minute grant, checks validity on tab resume, and makes one
passive POST /auth/refresh after a missing cold-bootstrap grant before requiring
sign-in. Renewal sends exactly {activity: boolean} as same-origin credentialed
JSON, with no bearer token on the auth endpoint.

Timers, API polling, focus/visibility resume and cold entry use activity=false.
Only fresh, trusted foreground pointer, keyboard, input, change or wheel events
can request activity=true. Such signals are throttled to at most one per minute.
A queued interaction is discarded when it becomes stale or the tab is hidden.
Background renewal cannot keep an idle browser alive indefinitely.

A 409 refresh_in_progress response gets one bounded Retry-After retry. Network
or provider failures retain the current form and memory session and use bounded
backoff. A definitive 401 or 403, or the immutable absolute boundary, ends
renewal. A cached idle end is only a snapshot: another tab may have extended the
same server session. Crossing that snapshot triggers passive BFF revalidation,
and the server rejects a genuinely idle-expired family. An accepted response
can replace the superseded idle end even if it arrives after that old boundary.
Nonremembered sessions retain their original five-minute expiry.

## App state and mutation safety

Renewal updates the existing Api instance. It does not rerun application bootstrap,
reset Benchmark Lab, replace the selected campaign, or clear a CSV File/preview.
The app shows a separate connection notice without rebuilding the form.

An authenticated GET rejected with 401 may renew and retry once. Business POST,
PUT and DELETE requests are never automatically replayed. Existing submission
idempotency keys and uncertainty handling remain in place. A mutation waiting for
preflight renewal is blocked if logout occurs before transmission. Responses
arriving after their short grant also pass the auth barrier before publication;
legacy expiry, absolute expiry and logout still block the result when a timer
has been delayed. This postflight barrier cannot retransmit a business mutation.

A local epoch, aborted fetches, and server generation checks discard late replies.
BroadcastChannel sends only a logout signal to other tabs, which immediately clear
their private view without automatically signing in again. Server SID tombstones
remain authoritative even if a late response's cookie reaches the browser.

## Validation

The Node tests use synthetic HTTP responses and deterministic clocks. They cover
cold entry, singleflight, contention, finite idle/absolute limits, trusted activity,
transient failure, logout races, no business mutation replay, CSV preview retention,
and selected Benchmark Lab campaign retention.

Within an allocated Karolina QA job, run the frontend package's Node test suite.
For actual synthetic browser checks, start test/visual-server.mjs with
MESOUQ_VISUAL_FIXTURES=1, then run test/firefox_session.py with /usr/bin/python3.11.
Set MESOUQ_SESSION_FIREFOX to an absolute executable shared Firefox path when
/usr/bin/firefox is unavailable on compute nodes; the resolved binary is recorded
in its evidence. The test uses its own temporary profile and loopback fixtures. It checks
real trusted browser input, passive renewal, file/units retention, cold entry and
cross-tab logout. Its screenshot and JSON evidence explicitly identify synthetic
data. It never logs in to AWS, Cognito or an HPC service. All fixture files are
excluded from the production build. The driver uses ordinary unnamed WebDriver
content execution for its test-only page globals. A named Marionette sandbox has
Xray wrappers that hide properties added by page scripts; this must not be
mistaken for a missing app fixture. Native ElementClick still supplies the trusted
foreground event, and all preview, idle-policy and logout assertions remain.
The loopback fixture's exact /logout route serves a fixed public signed-out page;
it ignores query values and never follows a supplied logout_uri or redirects.
See [Mozilla's Marionette script documentation](https://firefox-source-docs.mozilla.org/python/marionette_driver.html)
and its [content-command implementation](https://github.com/mozilla/gecko-dev/blob/master/remote/marionette/actors/MarionetteCommandsChild.sys.mjs).

Live authentication, the remembered opt-in and expiry behavior still require
the reviewed backend/frontend release and a separate production browser check.
This source change does not authorize security or infrastructure publication.
