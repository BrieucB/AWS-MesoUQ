# Private indentation browser v1

Source-only, dependency-free static browser for up to five invited MFA users.
Upload one force/displacement CSV with explicit unit and column metadata, choose
3.2, 3.4 or 5.8 μm nominal diameter, and submit individual curve-only calibration.
The scientific configuration comes unchanged from the shared
`src/aws_mesouq/presets/indentation-v1.json`, except for the selected diameter.
There is one operator-approved model, no scientific JSON editor, training,
population calibration, standalone prediction or model-release interface.

Results show ka, kb and d0 estimates with reported units and credible intervals,
fitted curve uncertainty, diagnostics, and conditional resonance. Resonance
propagates ka uncertainty with the approved polynomial bank’s fixed kb; inferred
kb uncertainty is excluded. The result/configuration declares interval probability
(approved preset: 0.9). Authenticated artifact downloads expose worker-returned
posterior samples, fitted-curve and resonance numerical data, and PNG/PDF figures.
Private filesystem paths are never fetched as downloads.

## Local checks

Use Node 20 or newer. There are no npm dependencies to install.

```sh
cd apps/aws_publishing
npm test
npm run build
npm run dev
```

The development server serves `http://127.0.0.1:4173`, including the shared preset.
Its checked-in runtime configuration is deliberately empty. It has no regional
authentication service and provides no local end-to-end scientific execution.
`npm test` uses synthetic responses and a minimal DOM harness; these tests do not
validate deployed AWS, MFA, scientific results, or live HPC execution. Optional
visual fixtures also use only synthetic data and are excluded from the build:

```sh
MESOUQ_VISUAL_FIXTURES=1 node test/visual-server.mjs
/usr/bin/python3.11 test/firefox_visual.py
```

The scientific runtime is external and private. Each user starts the terminal
connector with their own institutional account and the operator's project/site
configuration. Karolina and Vega retain the same contract; live Vega validation
is deferred. The API has no connector-status endpoint, so the browser explicitly
reports status as unavailable. No SSH passwords, keys or scheduler credentials
belong in the browser. Follow the operator runbook for connector installation,
approved model installation, and session authorization.

## Private hosting and sessions

The build emits self-contained static assets into ignored `dist/`, including
`presets/indentation-v1.json`. Supply deployment-specific `runtime-config.json`
there with `apiBaseUrl`, `cognitoDomain`, `clientId`, exact `redirectUri` and
`logoutUri`, and the allowed private S3 `uploadOrigins`. Optional `modelVersion`
pins a compatible released model when several exist; otherwise exactly one
compatible released model must exist. These are operator deployment identifiers;
none are committed here. Deploy only `dist/`, never the development server.

Entry is `/auth/login`. CloudFront signed cookies protect the entire interface:
HTML, JavaScript, CSS, runtime configuration and preset JSON. Only regional
`/auth/*` bootstrap endpoints are public. The regional service handles PKCE,
invitation and JWT verification, MFA via hosted identity, and absolute five-minute
HttpOnly host-only cookies. Enforce required MFA and invitations server-side.

The browser obtains a memory-only access token via credentialed, no-store
`GET /auth/session`, then fetches same-origin protected runtime configuration and
preset. Tokens are never persisted to browser storage, URLs or downloads. API
requests use bearer authorization with cookies omitted. Absolute expiry and API
401 clear state and return to `/auth/login`; invitation 403 shows denial without
a redirect loop. Sign-out clears local state, posts cookies to `/auth/logout`,
and follows only the validated configured identity logout URL. Failure offers retry.

Use HTTPS and no-store responses. Enforce independent JWT/project membership and
artifact authorization server-side. API CORS must allow the exact portal origin
and Authorization/Content-Type headers; S3 CORS must allow that origin, PUT/GET,
and the signed headers. Allow only configured presigned storage origins; bearer
tokens are never sent to S3. Completion verifies the uploaded original checksum.
Configure CSP with self-only scripts/styles, `object-src 'none'`, `base-uri 'none'`,
`frame-ancestors 'none'`, and `connect-src` restricted to self, API and artifact
origins. There are no remote assets, inline scripts, analytics or runtime mocks.

Job states, errors, diagnostics and owner cancellation come from the API.
Submission uncertainty requires connector/Slurm reconciliation before resubmission.
UI restrictions supplement the authoritative server admission checks.

This public source contains no model weights, real research curves/results,
private runtime configuration or live deployment. Preserve the repository MIT
license and upstream attribution when redistributing.
