# Benchmark execution contexts

The trusted deployment policy remains a flat mapping from project ID to policy.
An explicit project-record policy keeps precedence over the deployment fallback.
A new policy may include `execution_context_id`, a lowercase SHA256 identifier.
The browser cannot provide this field. Creation copies the entire policy into
the campaign's `approved_adapter`; changing a project default never rewrites an
existing campaign or its event/archive signatures.

For a new execution context, the private connector computes the ID as the
canonical checksum of:

```json
{"schema":"aws_mesouq.benchmark_execution_context.v1","operator":"<complete validated operator object>"}
```

The actual `operator` value is the complete JSON object, rather than the string
used in this illustration. Every source, adapter, environment, interpreter,
scheduler, identity, storage and upload-origin binding contributes to the digest.
The backend receives only the operator-approved digest and the existing public
policy pins. It does not receive the complete private operator configuration.
A digest selects a trusted registry entry; it does not authorize arbitrary
executables or replace validation of that entry.

For legacy campaigns whose frozen policy has no `execution_context_id`, the
portable selector is the canonical checksum of:

```json
{"schema":"aws_mesouq.benchmark_context.v1","project_id":"<campaign project>","science_revision":"<frozen commit>","adapter_sha256":"<frozen SHA256>","archive_root":"<frozen root or null>"}
```

The original policy remains unchanged. A missing archive root contributes JSON
null. Context resolution uses only the campaign's frozen fields.

A connector claim can include a signed `supported_contexts` list of 1–16
sorted, unique, lowercase SHA256 IDs. The server filters campaigns before DAG
refresh, expired-lease recovery, failure propagation, budget reservation, lease
creation or revision changes. A claim without this field can select only
legacy campaigns whose frozen policy has no context ID. A supplied list can
select the exact new ID or a legacy selector. An incompatible campaign stays
untouched. The typed FastAPI boundary and transactional service enforce the
same bounded list, including rejection of explicit null.

The client exposes `build_claim_body(...)` and `claim_frozen(body)`. The worker
must persist the complete unsigned body before sending it: request ID, campaign
target, exclusions and supported contexts are one immutable replay identity.
Signing uses a defensive copy and keeps the stored unsigned body unchanged.
The original `claim(...)` wrapper still builds a selector-free legacy request.
A pending legacy request must retain its original body; adding a selector under
the same request ID is an idempotency conflict. Cached replies retain their
original frozen policy even after a new default is published.

Backend support must precede claims containing selectors. An older backend
rejects the unknown field before leasing work; a caller must not retry by
silently removing the selector. Existing publication guards continue to require
the expected previous policy checksum, Lambda revision preconditions and exact
configuration readback. No enrollment, signing-key replacement, journal reset,
budget increase, scientific mode change or automatic rerun is part of this
contract.
