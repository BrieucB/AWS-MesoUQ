# Accuracy-target runtime optimization profiles

These options belong to a reviewed, source-bound accuracy execution context.
They are not browser controls. Omit the optional capabilities and their paired
fields to preserve legacy initialization/advance, serial factor scheduling,
reference-first guide execution and original source placement. Existing frozen
campaign contexts and their recovery paths are unchanged. See
[execution contexts](benchmark-execution-contexts.md) for registry selection and
hash binding.

## Capability selection

Add only qualified options to the context's existing `capabilities` list:

| Capability | Behavior and required profile |
| --- | --- |
| `fused_factor_lifecycle_v1` | Fresh `hnpe_initialize_advance` initializes and advances one factor in one native process. |
| `bounded_factor_parallelism_v1` | Authorizes frozen waves of at most three independent one-GPU factor tasks. |
| `independent_guide_reference_v1` | Runs independent guide reference work alongside method work after common preparation. |
| `cpu_reference_resources_v1` | Requires context `reference_resources` and operator `reference_scheduler`; applies only to guide/confirmation references. |
| `verified_source_staging_v1` | Requires operator `source_staging`, exact `native_package_resources` and the approved staging helper in `native_python_sources`. |
| `persistent_execution_cache_v1` | Requires operator `execution_cache`, configured before the first JAX import. |

Fusion retains the ordinary durable initial generation and continuation receipts.
They share one authoritative Slurm allocation, so charge that allocation's
allocated GPUs times actual elapsed time once, including startup, imports,
validation and finalization. Generation diagnostics are not separate allocations.
A later resumed allocation has its own cost. Unknown costs stay unresolved.

Factor scheduling admits a source-authorized wave, reserves each factor against
both its remaining elapsed allowance and the total method GPU-second ceiling,
then waits for all owned children to be terminal and accounted before replanning.
Factor state, RNG stream and checkpoints stay independent. Completed tasks release
their GPU allocation promptly; a wave barrier may leave scheduling capacity unused.
This is separate one-GPU allocation scheduling, not shared-GPU co-residency.
Cancellation and ambiguous-submission reconciliation retain their existing
submission intents and reservation holds.

Reference overlap starts only after common case plans and banks are frozen.
Method outputs may finish first, but guide accuracy scoring, stopping decisions
and sealing wait for the authenticated reference join. Confirmation still joins its reference
and both sealed methods. Reference costs remain separately reported.

## Source placement and executable cache

The following is a partial operator example, using generic paths and illustrative
bounds rather than a deployable registry entry:

```json
{
  "source_staging": {
    "root": "/var/tmp/mesouq-source",
    "max_bytes": 67108864,
    "max_namespaces": 4
  },
  "execution_cache": {
    "root": "/var/tmp/mesouq-executables",
    "max_bytes": 268435456,
    "max_namespaces": 4
  }
}
```

Provision canonical, account-owned private roots without symlinks or permissive
default ACLs. Keep staging and executable-cache roots disjoint from each other
and from approved source trees; neither root may contain the other. Keep run
state, datasets, weights and archives outside these code/cache roots.
Changing roots or bounds changes the complete operator checksum and requires a
new reviewed context, not editing an existing campaign.

Staging authenticates the complete Python source closure and all non-Python
package resources, excluding bytecode. `native_package_resources` maps each
original absolute package-resource path to its SHA256. Original and staged
source membership and bytes are checked; staging does not replace full source
verification with metadata caching. Staged code is read-only and its lease lasts
for the native process.

Use the same stable absolute staging base on every relevant compute node.
The runtime-profile-qualified namespace must resolve to the same absolute code
path across jobs: native checkpoint layouts can bind source paths. Do not add a
job ID to that base or relocate a live lineage to a different import path.
Node-local capacity is provisioned and verified per node.

The two `max_bytes` fields have different meanings:

| Profile | Byte and count limits |
| --- | --- |
| `source_staging` | **Total root** logical bytes across all admitted namespace payloads and manifests; `max_namespaces` also bounds root admission. Bounds: 1–536,870,912 bytes and 1–64 namespaces. Full/unavailable or unsafe staging fails closed; there is no silent original-source fallback or automatic eviction. |
| `execution_cache` | **Per-namespace** budget; namespaces bind scientific source and runtime identities. Bounds: 0 (explicitly disabled), or 65,536–1,099,511,627,776 bytes, and 1–64 namespaces. Root exposure scales with namespace count plus bounded root metadata. Inspect `maximum_root_budget_bytes` and `native_lru_bytes` in the receipt; the native payload LRU uses a conservative fraction of the namespace allowance. |

Cache policy is fixed at the root. Admission may return
`disabled_admission_busy`, `disabled_native_cache_busy` or
`disabled_namespace_capacity` without deleting another namespace. Interrupted
policy/namespace publication returns `disabled_incomplete_policy`,
`disabled_incomplete_namespace` or `disabled_pending_namespace` with
`repair_required: true`; preserve evidence and arrange an explicit offline,
owned-state repair. No automatic deletion or root-policy replacement is implied.
Unsafe state raises a configuration error. Source staging capacity and lease
errors also require operator resolution.

An enabled cache is a performance aid. Its receipt states
`cache_read_error_policy`; the native read-error policy warns and compiles from
the current HLO. Check native cache `requests`, `hits`, `write_attempts` and
`lookup_misses`; unobserved counters are unknown, not zero, and write attempts
are not confirmed writes. Do not infer a speedup from cache admission alone.

## Optional CPU-only references

CPU-only references require all three additions together:

- Context capability `cpu_reference_resources_v1`.
- Context `reference_resources`, for example
  `{"native_cpus": 8, "native_gpus": 0}`; CPUs cannot exceed `native_cpus`.
- A reviewed operator `reference_scheduler` containing `partition`, `account`,
  `qos` and `mem`, with a verified Slurm association accepting zero-GPU work.

A queue name does not establish permission or allocated resources. If the
account requires a GPU allocation for reference work, leave these optional fields
and capability absent. CPU reference algebra in a one-GPU allocation still incurs
that allocation's full GPU cost. Zero-GPU references retain positive allocated
walltime and truthful CPU accounting; they do not receive extra method funding.

## Deadlines and qualification

All options retain the original Slurm start and funded hard end. Source staging,
cache setup, process startup and validation consume that same allocation; fusion
or concurrency does not restart or extend clocks. Per-factor elapsed caps remain
distinct from the total method ceiling. Retain reservations until every owned
child is terminal and its actual allocation cost is resolved. See
[native allocation clocks](benchmark-allocation-clock.md).

Source tests establish contracts, not live scientific equivalence or speedup.
Qualify fresh and resumed state, complete validation banks, actual allocation
accounting and cancellation before publishing a new context. Shared-GPU pilots
need their own state, memory and timing evidence and do not enable production
co-residency.
