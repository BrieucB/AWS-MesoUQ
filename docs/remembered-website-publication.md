# Publish remembered-browser website assets

The remembered-browser frontend adds `src/site-session.js` and changes only
`src/client.js`, `src/style.css` and `src/app.js` relative to the existing
Benchmark Lab static release. The older posterior and Benchmark Lab publication
profiles intentionally reject this delta.

Use the manual `publish-remembered-website.yml` workflow after reviewing the
exact main source, current live baseline and backend/session activation plan.
The default pre-feature baseline is `7cf0e5307ddcbc804d0b93ce4fb8d3fe4e7fa37c`;
the operator must verify that it describes the deployed source rather than
accept the default as evidence.

The `remembered-session` profile preflights every existing asset plus the new
module, then conditionally publishes the module, client, stylesheet and app entry
in that order. It requires exactly 14 build assets, including all existing
Benchmark Lab/posterior resources. Unexpected build deltas fail before identity
or S3 access. All live objects must match either the reviewed baseline or target
bytes and their private/no-store/encryption/checksum metadata before any write.
An existing foreign new module fails closed. The private runtime configuration
is read and fingerprinted but never written. The complete target is read back,
including runtime/configuration ETag stability.

The OIDC session can read those 14 exact object keys and write only the four
reviewed paths; bucket listing is scoped to the existing website bucket. It
adds no account-role action, wildcard object permission, delete permission,
CloudFront administration or new publication identity. Existing posterior and
Benchmark Lab workflows and their scopes remain unchanged. The same protected
GitHub environment, pinned credential action, exact-main admission and shared
website-publication concurrency group apply.

The workflow builds the exact current and baseline frontend sources, then runs
the actual Node build-pair publication preflight with fake AWS before requesting
ephemeral credentials. Normal CI runs the synthetic protocol/race/permission
tests; allocated Karolina QA supplies the real build pair too. This source and
fixture evidence does not establish live authentication or CloudFront behavior.

A failed conditional write leaves its verified prefix in place; reconcile the
same baseline and target rather than issue unconditional writes or choose a new
baseline to bypass a mismatch. An exact-target retry performs no writes. Keep
the source baseline for such retries even after the new module exists live.
Backend/session rollback uses the reviewed disabled feature flag and preserves
the original native client; do not delete the new static module or overwrite
runtime configuration as a shortcut.

See [Remembered browser sessions](remembered-browser-sessions.md) for the
separate owner-approved Cognito, audience, TTL and viewer-hook changes and live
acceptance gates. Publishing these static assets does not activate that feature.
