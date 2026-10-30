# Native allocation clock

New Benchmark Lab execution contexts use the versioned allocation_deadline_v1
policy. Their immutable worker manifest contains the funded native allocation
in allocated_seconds. The generated batch script captures Slurm's job ID, start
timestamp and projected end timestamp in readonly variables before loading the
scientific environment.

The adapter checks those captured values against the native environment and
sets the effective hard end to the earlier of the projected end and start plus
the funded allocation. Startup, source verification and imports consume that
same allocation. A projected extension cannot add computation to the reserved
method budget. Epoch time is converted to a monotonic watchdog once so a later
wall-clock adjustment cannot extend its interval.

The scientific CLI receives all three native identity/time fields. It starts
its clock before heavy scientific imports, stops successful publication at
least 30 seconds before the hard end and reserves 120 seconds for HNPE
validation and finalization. The relative scientific control remains an
additional ceiling. These margins consume the existing allocation; no new GPU
budget is added.

The adapter supervises the scientific child in a private process group until
five seconds before the hard end. A timeout or supervisor SIGTERM/SIGINT terminates and, if necessary, kills
the group. Startup before adapter entry is bounded by Slurm itself; it has no
independent adapter watchdog. A finite startup delay consumes the absolute
deadline, while hung setup can still end in an honestly accounted native TIMEOUT. This bounds cooperative work and provides fail-stop behavior for a
hung native operation. It cannot guarantee a valid checkpoint or receipt after
arbitrary filesystem, GPU or process failure. Native Slurm state and actual
allocated accounting remain authoritative; a TIMEOUT is never a successful
calculation merely because a scientific receipt says completed.

## Legacy recovery

The old public adapter is bundled byte-for-byte as benchmark_legacy.py, pinned
by its SHA-256. A trusted registry selects only a bundled adapter by exact hash.
Original operator files, staged manifests, scientific source checkouts and old
campaign policies are preserved. Existing campaigns retain their original
execution context and results.

New source contexts are selected by a signed capability before the backend
leases a stage or reserves computation. The normal connector migrates its
existing journal atomically while retaining the old context and recovery
records. Older connectors reject the upgraded journal and cannot claim the new
context. No key replacement or reenrollment is needed.

## Validation

Focused checks exercise delayed startup, exhausted allocations, changed native
identity, projected-end clamping, immutable clock records, monotonic watchdog
behavior, process-tree termination and exact legacy snapshots. Their old-source
counterparts are retained for regression evidence. A separate native proof must
use a fresh production-shape factor with an imposed startup delay, valid
checkpoints and successful publication before the deadline, with every actual
allocation charged and reported. Synthetic checks alone do not qualify the
browser-to-Karolina benchmark.

[Slurm native start/end environment variables](https://slurm.schedmd.com/sbatch.html)

## Portable termination and group ownership

Cleanup has one four-second monotonic budget, including process metadata and
reaping, within the existing five-second watchdog margin. The supervisor checks
Linux/macOS process-control capabilities and default SIGCHLD before launching a
scientific child. Before the first group signal, waitpid with WNOHANG must prove
a live owned child whose PID, process group and session identify the private
group. An already exited/reaped child or ambiguous ownership fails closed
without signalling a potentially recycled group.

After SIGTERM, the supervisor keeps the owned leader unreaped through the
bounded grace period and SIGKILL. A TERM-resistant descendant therefore still
receives escalation when its leader exits. Only after the final group signal
and bounded confirmation of no live members does the supervisor reap its child.

Darwin can report EPERM for a group containing only zombies. Only Darwin EPERM
can use the quiescent-group fallback; EACCES and non-Darwin denials remain errors.
The fallback is accepted
only when bounded exact-group metadata proves every member is a zombie, or a
zero-signal probe proves that the group no longer exists. Live members, foreign
ownership, malformed/partial/oversized output, missing tools and denied metadata
remain failures. Metadata reads stop at 64KiB plus one overflow byte, and the
isolated metadata child is killed/reaped within its own query budget.
Process metadata requests contain PID, PGID, state and UID;
they contain neither command lines nor environments.

The selectors intentionally differ: Apple ps uses `-g PGID`, whereas procps uses
the documented negative numeric PGID selector. Linux `-g` selects sessions and
is unsuitable here. Python waitid/WNOWAIT is not assumed available on macOS.

Sources: [Apple ps manual](https://github.com/apple-oss-distributions/adv_cmds/blob/main/ps/ps.1),
[procps ps manual](https://github.com/procps-ng/procps/blob/master/man/ps.1),
[Python3.11 process waits](https://docs.python.org/3.11/library/os.html#os.waitpid),
[Apple XNU group signalling](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/kern/kern_sig.c).
