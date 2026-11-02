# Shared benchmark dispatch

The registered benchmark-all worker handles fixed-budget and accuracy-target campaigns in one process. It shares the normal enrolled journal, API token client, Slurm interface and existing guarded SSH transport. The authentication controller stays alive when this worker is replaced.

The accuracy-context registry is an explicit operator input, read through the existing typed runtime interface. Fixed-budget mode remains the default and does not import the accuracy adapter. Worker source still requires the complete approved package binding and registered entrypoint.

The dispatcher alternates synchronous protocol sweeps and saves its next turn before each call. A failed protocol does not starve the other; authentication and transport failures stop new work. Stop acts between completed sweeps and never implicitly cancels Slurm jobs. An active count of zero does not establish an empty queue.

Per-protocol coordination windows provide operational backpressure. Slurm enforces physical resources and the declared method budget remains the scientific comparison limit.

Mixed mode opts into immediate propagation of expired authentication and HTTP401/403 from the legacy worker; its default independent mode retains its prior error policy. Both protocols stop on guarded SSH failures. The accuracy worker always propagates expired authentication.

To use mixed mode, the controller must start with the explicit reviewed accuracy registry. A controller started in pure fixed-budget mode cannot acquire new registry authority through a worker replacement command; a rejected mixed upgrade leaves its existing worker and authentication session running.

Before native submission, accuracy mode rechecks the exact task after slow lookup/preflight and requires the same live lease epoch. If the original check-cycle authority or durable-write interval expires, it retains the intent and unresolved accounting without invoking Slurm or fabricating zero cost.
