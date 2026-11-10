# Accuracy-target campaigns

Accuracy-target campaigns compare TMCMC and a newly trained HNPE lineage using the approved analytical hierarchical framework. Scenario family/scale, seed, bounded HNPE training and architecture controls, TMCMC sampler controls, and the method GPU-second ceiling are validated by the schema version2 contract.

The immutable profile w2-rms0p1-central90-2048-244of256-v1 uses eight guide worlds, 256 independent confirmation worlds, the central90% cohort, and 2,048 posterior draws. Guide worlds select effort through the bounded ladder; confirmation data do not select that effort. The observed244/256 criterion is assessed separately from the exact-binomial population-confidence claim. Marginal accuracy does not establish joint posterior equality, reference refinement is not an exactness proof, and one HNPE lineage does not measure training-seed variability. Failed and unresolved cases retain their denominators. The interface displays source-supplied limitations and verdicts unchanged.

## API and execution

The authenticated project API is rooted at /v1/projects/{project}/benchmark-accuracy. It provides capabilities, campaigns, task pages, cancellation, archival, accounting, results and private artifact descriptors. Signed connector claims/events use /v1/benchmark-connector/accuracy. The API validates and stores source results; scientific calculations and performance measurements run on Karolina.

The shared owner controller uses its existing enrolled journal/key and a reviewed typed execution-context registry. The benchmark-all worker alternates fixed-budget and accuracy-target work. See [Shared benchmark dispatch](benchmark-dispatch.md) for authentication, lease and registry boundaries. Coordination windows provide backpressure; Slurm enforces physical resource availability. Runtime capability/profile selection and its accounting/storage rules are described in [Runtime optimization profiles](benchmark-runtime-optimizations.md).

## Costs, results and recovery

Method costs include allocated startup time, HNPE training and inference, and failed or resumed method attempts. Independent reference and overhead costs are reported separately. Reservations and allocations with unresolved costs remain explicit; they are not converted into zero cost.

The bounded result envelope carries the source assessment and authoritative accounting. The full comparison JSON is a separately verified private artifact, alongside curated numerical files and figures. Verified partial files can remain available after a failed or cancelled task.

The first complete scientific outcome is immutable. Leases and durable submission intents are rechecked before Slurm submission. A new allocated comparison-recovery attempt changes ledger membership and cannot silently adopt an earlier frozen comparison: this case fails closed while preserving the original output and all costs. Keep the evidence and request archival through the normal workflow.

## Acceptance evidence

Source/API/SDK tests, synthetic worker-process proofs and hosted CI establish software and wire-contract behavior. Private dependency/device validation, genuine native GPU proof, the signed browser-to-AWS-to-Karolina campaign, result display, verified downloads and PROJECT archival are separate release/acceptance gates. Record the native and live gate evidence separately before project acceptance.


### Source-policy qualification for new work

An accuracy context may carry `source_qualification`, a trusted operator
record with exactly these fields:

| Field | Meaning |
| --- | --- |
| `schema` | `aws_mesouq.benchmark_accuracy_source_qualification.v1` |
| `science_revision` | Exact scientific Git revision |
| `source_binding_sha256` | Exact authenticated scientific source closure |
| `semantic_policy_sha256` | Policy fingerprint computed by that source |

The server project policy and private connector registry deliver the same
record. It is not a campaign request field. Copying an existing context policy
pin into a record does not qualify its source. The normal native
`qualify_source` operation installs the authenticated source guard and calls
the scientific module's unmodified `policy_fingerprint()`. On the qualified
HPC host, `tools/qualify_accuracy_source.py` uses that operation and requires
the computed result to match the proposed context before writing a new private
qualification receipt. It takes no client-supplied expected policy hash, opens
no SSH connection, and does not publish, enable, or edit any context.

An absent record remains absent when validating legacy contexts; it does not
change their canonical bytes or IDs. Adding a qualification changes the new
context's ID. Unqualified contexts remain available for historical observation,
accounting, cancellation, artifact recovery and archival. They cannot authorize
new reservations, claims, preparation or submission. Final capsule policy
validation remains unchanged.

For a guarded rollout, first review and test the code, then independently
derive the proposed policy identity from the exact qualified scientific
source. Generate and review the private qualification receipt on HPC and
check its source tuple against the proposed context. Retain previous contexts
and evidence; do not rewrite their request, manifest, control, clock or costs.
Publish the reviewed backend, native source and private policy/registry under
an explicit owner release approval, then admit the new worker through the
normal package/source checks. Verify that replay and historical recovery work
before authorizing new work. An existing campaign has no context-migration
operation: starting a corrected campaign requires a separate owner decision.
