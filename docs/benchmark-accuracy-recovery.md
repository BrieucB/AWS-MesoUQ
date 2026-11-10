# Accuracy campaign cancellation recovery

A cancellation can arrive after the backend acknowledges submission intent but before the worker invokes the scheduler. The previous contract can then wait indefinitely for a producer that it cannot identify. The application must expose that unresolved state and preserve honest accounting.

A local journal flag, original event acknowledgment, current lease and exclusive lock do not provide anti-rollback evidence. A restored pre-invocation snapshot can look identical after a real submission. Therefore legacy attempts cannot be closed at zero cost using those facts or a missing scheduler record. The existing never-submitted guard remains unchanged.

## Versioned invocation authorization for future tasks

Only newly created, explicitly version-bound tasks use the new protocol. Existing tasks retain their original protocol and identities.

1. The backend records submission intent under the ordinary signed claim and event contract.
2. Before any native scheduler invocation, the worker requests a server-side invocation authorization bound to the exact task, attempt, manifest, allocation, connector, source, lease epoch and revision.
3. The backend grants authorization atomically only while submission remains allowed. Cancellation that precedes a grant prevents every later grant.
4. When cancellation occurs before any grant, the backend can establish from its own durable state that invocation was never authorized. A separate typed abort event records provenance and closes that future allocation atomically. Local False flags and scheduler absence are not the authority for this conclusion.
5. Once a grant exists, the attempt retains normal reconciliation and authoritative terminal-cost requirements. A lost grant response, worker restart or replay cannot create new permission to invoke the scheduler again.
6. The worker durably fences an abort before sending its closure event. The fenced state cannot reach native submission after restart or event replay.

Old workers must fail before submission when presented with new-version tasks. Reconstructed claims, wrong bindings, stale/transferred leases, malformed values, conflicting events and replay mismatches fail closed. Exact event replay must remain stable. The accepted-submit/lost-response window remains an explicitly unknown state; timeouts never convert it into zero.

## Application behavior

The browser explains why cancellation or archival is waiting, distinguishes reservations from settled consumption and continues polling when cleanup or a requested archive remains incomplete. Cancellation is unavailable after it has already been requested. Public diagnostics use fixed safe messages and never display journal, operator or raw request contents.

Policy qualification remains a separate admission requirement. Enabled qualified-context publication binds to SHA-pinned receipts from the source-derived qualification tool. Unchanged legacy field absence and historical context identities remain unchanged. Observation, cancellation, reconciliation, artifact reads and archival remain available for existing work.

## Acceptance matrix

| Scenario | Required outcome |
| --- | --- |
| Legacy cancellation before submission intent | Existing never-submitted contract unchanged |
| Legacy post-intent entry with False flag and no producer | Diagnosed unresolved state; costs and reservation preserved |
| New-version cancellation after intent and before any invocation grant | No later grant; separately typed atomic abort closure |
| Invocation grant already recorded | No zero-cost inference; original-producer reconciliation |
| Old worker on new-version task | Rejected before native submission |
| Expired/transferred lease, wrong connector/source or mismatched bindings | Authorization/abort rejected; no accounting changes |
| Cancellation/grant race | Exactly one atomic ordering; no grant after successful abort |
| Lost response, restart or restored pre-grant snapshot | No second native invocation; exact replay stable |
| Original producer later identified | Cancel/reconcile that producer with authoritative terminal accounting |
| Qualified policy record without matching pinned source-derived receipt | Publication rejected before deployment |
| Unchanged enabled or disabled legacy context | Original identities and recovery access preserved |
| Cancelling/blocked campaign with unfinished cleanup or archive | Safe reason visible and polling continues |
| Scientific result publication | Original policy, denominators and capsule guards unchanged |

## Release and live recovery gates

Implementation and synthetic tests run on Karolina. The audit does not inspect a real connector journal, operator body, scientific array, key or raw log. The original campaign and its reservation remain unchanged during preparation.

Before live rollout, independently review the exact candidate, run the relevant backend/SDK/native/frontend regression and packaging checks, and bind the release artifacts to that candidate. Deployment, connector replacement, source qualification/context activation and any live recovery action follow their separately approved release scopes.

The existing campaign's actual local phase remains unverified, and it does not acquire the future protocol retroactively. Its closure still needs the same original producer with authoritative terminal accounting, or a separately approved evidence-backed recovery decision. Normal PROJECT archival requires terminal allocations, zero reservations and zero unresolved accounting, followed by independent inventory and readback verification. A private code fix or diagnostic does not satisfy those live acceptance gates.


## Wire contract and release order

The trusted context capability is `server_invocation_fence_v1`. Only newly created tasks from such a context receive `submission_protocol: server_invocation_fence_v1` in their immutable manifest. Absence is preserved for legacy records. The signed `authorize_invocation` event carries a fresh 32-hex `grant_request_nonce` and the 64-hex `submission_intent_sha256`. The server checks the current lease, revision, task, attempt, allocation, connector and original submission intent before granting authority. `invocation_authorization_fresh` is true only for the first applied authorization; a replay is never a fresh grant.

Prepare and independently verify the backend, SDK/worker and UI as one compatible candidate. Deploy support before enabling the capability on any future qualified context. The old worker must reject the new manifest field rather than silently submit it under legacy behavior. Preserve observation, cancellation, terminal accounting, artifact reading and archival for existing tasks throughout an upgrade. Publisher activation requires the exact qualification receipt checks documented in [source-policy qualification](source-policy-qualification.md).

A source candidate and synthetic tests do not authorize source publication, a live rollout, qualification generation, context activation, a replacement campaign or a claim about the original unknown allocation. Those are separate release decisions. Until approved, the published runtime stays unchanged and monitoring continues. For the existing campaign, the normal archive gate still requires terminal status, no reservations and no unresolved allocations; any exception requires a separately reviewed recovery contract and explicit owner decision, with no invented zero-cost accounting.


### Campaigns with no native allocations

The existing archive contract also requires a genuine native allocation. If every future allocation is cancelled before authorization, the server ledger can reach cancelled status with zero reservations and no unresolved allocations, but the normal native archive remains unavailable because there are no native outputs to archive. Preserve the normal campaign JSON and its typed server-abort accounting; do not fabricate a native job, output, archive verification or scientific capsule. A cancelled campaign with genuine prior native outputs remains subject to the normal complete accounting and archive verification gates. This distinction does not resolve a legacy allocation whose submission remains unknown.
