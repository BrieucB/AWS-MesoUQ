# Shared SSH connection control

Use the tested connector interpreter and package path from the existing release.
Do not create another enrollment or copy keys or journal contents. The transport
state described here is separate from the enrolled connector journal.

## Operating sequence

1. Stop unrelated automatic SSH retry clients. Every project actor, including
   manual diagnostics and transfers, must use the same approved shared session.
   An existing raw orchestration SSH connection must finish before a new
   controller starts. Do not close unrelated Vega sessions.
2. Set AWS_MESOUQ_SSH_STATE_DIR to one canonical, account-owned private directory
   under the approved connector installation. All participating processes and
   runtime versions must use that exact directory. It must have mode0700 and a
   short path suitable for a Unix control socket.
3. Before the first controller startup, append any known manual connection
   attempts from the preceding hour using record-attempt. Supply their UTC epoch
   timestamps. Import successes and failures; never reset or hand-edit the ledger.
4. Start the foreground hold command once. Keep its process open. It owns one
   authenticated SSH master and never retries a failed startup automatically.
5. Start the connector with the same state-directory environment. Transport
   borrows the existing master only. Other actors use the guarded run command,
   rather than raw SSH, SCP or SFTP.
6. Stop connector activity before stopping hold. Ctrl-C and SIGTERM reap only the
   owned master. SIGKILL or an uncertain crash leaves a durable tombstone that
   blocks automatic replacement.

Set mesouq_python to the pinned connector interpreter and use the approved
release's package environment before running these commands:

~~~sh
export AWS_MESOUQ_SSH_STATE_DIR="/absolute/private/connector/ssh-state"

"$mesouq_python" -m aws_mesouq.connector.ssh_session \
  --state-dir "$AWS_MESOUQ_SSH_STATE_DIR" status

# Run once for each still-relevant manual attempt, using its actual epoch:
"$mesouq_python" -m aws_mesouq.connector.ssh_session \
  --state-dir "$AWS_MESOUQ_SSH_STATE_DIR" record-attempt --at EPOCH_SECONDS

# Keep this foreground process open; do not put it in an automatic retry loop:
"$mesouq_python" -m aws_mesouq.connector.ssh_session \
  --state-dir "$AWS_MESOUQ_SSH_STATE_DIR" hold --host karolina
~~~

Other processes can then borrow without admitting another connection:

~~~sh
"$mesouq_python" -m aws_mesouq.connector.ssh_session \
  --state-dir "$AWS_MESOUQ_SSH_STATE_DIR" run --host karolina -- id -un

# Explicit stdin forwarding is bounded to32MiB and preserves bytes.
"$mesouq_python" -m aws_mesouq.connector.ssh_session \
  --state-dir "$AWS_MESOUQ_SSH_STATE_DIR" run --host karolina --stdin -- cat
~~~

Use the operator-approved alias consistently. The resolved account, host and port
are bound to the session. The master uses strict existing host-key verification,
no agent forwarding, no local-command hooks, no automatic retry, and no proxy jump.
Borrowers ignore SSH configuration and use the recorded target and a forced false
proxy. A missing or dying control socket therefore cannot fall back to a fresh
TCP connection. An earlier check of the socket alone would not prevent that race.

## One literal endpoint per admitted attempt

The controller resolves the effective logical host once and selects one literal
IPv4 or IPv6 address, respecting AddressFamily. It never falls back to another
DNS result. The exact numeric HostName and address family are passed to the
master, with canonicalization disabled. The logical hostname/user/port remain
the session binding; the endpoint is separate strict schema2 metadata.

Host-key verification retains the effective HostKeyAlias, including an explicit
literal alias of none, or the original hostname when no alias is configured.
Before admission, two configuration-only OpenSSH probes compare all effective
options, including repeated identities and trust files, apart from the intended
hostname/address-family/key-alias overrides. Unsupported changes fail closed.

The initial direct profile supports port22 and trusted SSH configuration with
canonicalization, CheckHostIP, host-key DNS verification and GSSAPI authentication
disabled. Host-dependent percent tokens in authentication/trust paths and
effective local/known-host hooks are unsupported. Do not rewrite keys or trust
files to satisfy these checks. OpenSSH configuration parsing is trusted: Match
exec can execute during a configuration probe, before effective options exist.
Operators must verify that applicable configuration has no such execution hooks.
This module does not sandbox arbitrary SSH configuration or other programs.

Old transport-ledger schemas are rejected; never delete a ledger to change its
schema. These transport files are independent of the enrolled connector journal.

## Attempt budget and failure handling

The durable ledger admits at most three successful or failed new attempts in any
rolling3600-second interval. A cross-process lifetime lock permits one master.
Reservations are fsynced before spawning SSH; normal restarts retain allowance.
Attempt times are conservatively settled after authenticated readiness. Failed or
uncertain records are extended only after recovery proves the previous processes
dead; interrupted pre-ready cleanup settles after its owned master is reaped.
Slow configuration, DNS, scheduling, or spawn timing cannot shorten the allowance.
Corruption, deleted initialized state, clock rollback and uncertain owner/master
state fail closed. No token bucket, process-local counter or file deletion is a
substitute for this ledger.

A transport failure latches a shared circuit and stops later stage polling,
claims and archive calls. Ordinary nonzero remote commands remain compatible with
missing-file and immutable-create probes. Permanent archive integrity failures
halt automatic sweeps. Timestamped errors and status expose sanitized categories
and counters without credentials or scientific payloads.

After an operator verifies the prior controller and master are gone:

~~~sh
"$mesouq_python" -m aws_mesouq.connector.ssh_session \
  --state-dir "$AWS_MESOUQ_SSH_STATE_DIR" recover
~~~

Recovery does not reset the attempt history. Unknown spawn identities and
unexplained orphan sockets require operator review; do not delete state to force
another connection. Source submission intents and lost-acknowledgment recovery
remain authoritative; a network failure never authorizes resubmission.

## Scope

This controls participating clients using the shared directory. It cannot
intercept arbitrary raw SSH programs outside it. Keep independent retry services
disabled, and do not give each agent a separate state directory or connection
allowance. Slurm computation concurrency remains independent of SSH admission.

Each remote command still uses a multiplexing client process over the same
authenticated connection. Physical handshake counts and remote RPC counts are
different measurements. Scheduler and archive batching reduce RPC overhead
separately; do not report them as implemented merely because reuse is enabled.
