# Preserve login-form Origin under privacy headers

The public sign-in page submits an ordinary same-origin HTML form. A no-referrer
document policy makes a non-CORS form POST serialize its Origin as null. The
backend correctly rejects that value before processing the login intent.

The generated login document declares a same-origin referrer policy before its
form. This document-only policy preserves the exact origin on the form POST and
sends no Referer on cross-origin navigation. The HTTP no-referrer headers, exact
Origin requirement, null-origin rejection, CSRF intent, CSP, SameSite cookies and
private asset authorization remain unchanged.

Allocated QA runs the real Firefox form-navigation regression with the same
no-referrer response header against local synthetic fixtures. It exercises the
unchanged product Origin guard and checks that a second-origin navigation
receives no Referer. The pre-change source must report a null form origin; the
candidate must report the exact local origin. The fixture never starts OAuth,
exchanges tokens, reads user browser state or invokes a business endpoint.

Set AWS_MESOUQ_LOGIN_TEST_FIREFOX to the reviewed shared Firefox executable and
AWS_MESOUQ_LOGIN_BROWSER_OUTPUT to a fresh evidence directory when invoking the
focused browser test. Without that explicit executable, normal portable unit
runs skip the allocated-browser test; a skipped run does not qualify as browser
proof. Retain the PNG beside the generated inline-image report and JSON.

References: [Fetch Origin construction](https://fetch.spec.whatwg.org/#append-a-request-origin-header)
and [HTML referrer metadata](https://html.spec.whatwg.org/multipage/semantics.html#meta-referrer).
