# Connector authentication controller and replaceable workers

The library keeps the normal Cognito token session alive while workers are
replaced. The existing CLI and running connector are unchanged. Public CLI
integration, packaging and live rollout have their own review.

## Controller and worker interfaces

AuthBroker receives the existing PKCE login factory, such as a callable invoking
browser_login(issuer, client_id, port). Its start method invokes that factory once.
The returned normal TokenSession and its refresh credential stay in controller
memory. Issuer, client, scopes, refresh rules and expiration behavior remain
those of connector.auth. No Cognito configuration changes.

BrokerClient.from_environment() is the worker's token-session object for the
unchanged API(base_url, token_session) constructor. It implements the normal
token(force_refresh=False) interface and the API's 401 refresh retry. A worker
cannot initiate a login or retrieve a refresh credential.

SourceWorkerSpec.from_manifest(package_root, manifest, approved_binding_sha256,
arguments=...) registers a complete reviewed source-only package. The manifest
has exactly schema, entrypoint, files and binding_sha256, with schema
aws_mesouq.approved_source_worker.v1. Its files map covers every regular runtime
file, including the entrypoint, by SHA-256. Obtain the approved binding separately
from the reviewed release workflow. Computing a candidate digest does not approve
that candidate.

Use a private versioned release directory. Files and directory ancestors require
trusted ownership and no group/world write permission. Symlinks, bytecode caches,
unbound files and hardlinked code files are rejected. The interpreter, standard
library and installed third-party dependencies are trusted runtime dependencies
outside this source-package binding.

WorkerSupervisor.start() validates the package before starting authentication.
restart_worker() replaces only the registered worker process. apply_spec() is the
integration hook for an owner-controlled reviewed-release loader. A bad candidate
is rejected before stopping the old worker. A fixed fresh-interpreter bootstrap
checks the complete package again. Its bounded stdin manifest is the startup
gate, released only after PID and generation registration. There is no import
hotpatch or arbitrary shell command.

Worker arguments are the existing approved SDK runtime arguments, including the
same operator and journal paths. The supervisor does not read the operator or
signing key, create an enrollment, cancel scheduler jobs, or manage an SSH master.
Future CLI integration must preserve the shared SSH guard and normal journal
reconciliation. Graceful termination, and any timeout escalation, target only
the exact owned worker PID. No process-group kill is used.

Only nonsecret coordination metadata enters the child environment:
AWS_MESOUQ_AUTH_SOCKET, AWS_MESOUQ_AUTH_CONTROLLER_PID and
AWS_MESOUQ_AUTH_GENERATION. OAuth credentials never enter files, arguments,
environment variables or broker logs. Worker stdout/stderr default to the null
device. A future CLI should publish explicit credential-free progress metadata.

## Security boundary

The Unix socket has mode0600 inside a canonical private0700 directory. Descriptor
walks reject symlinks and unsafe ancestors. Root-owned sticky temporary directories
are the sole shared-write ancestor exception. Any existing socket-path object is
refused and preserved.

Both endpoints verify kernel UID and PID. Only the registered live worker and
current generation can request the short-lived access token. Authorization checks
the original owned Popen instance, preventing a recycled PID from becoming that
worker. Socket RPC cannot register workers, replace packages, restart processes,
initiate a login or retrieve refresh credentials.

Length-framed typed JSON has bounded request/response sizes, connection counts and
read time. Duplicate/unknown fields, nonfinite numbers and unknown operations are
rejected. Refresh calls are serialized. Epochs and a minimum interval coalesce
repeated forced refreshes. Status and errors contain controlled metadata rather
than exception, request or credential text.

Fencing stops new retrieval; it does not revoke an access JWT already delivered.
The approved worker is trusted to handle that credential. A malicious approved
worker can misuse it. Controller/code/runtime compromise and same-user OS
debugging privileges are outside this boundary. Same-user path interference can
cause denial of service; peer checks fail closed rather than trusting another
server.

Ordinary worker replacement preserves the controller session. Cold controller
start, genuine refresh expiration or revocation can still require owner sign-in
and MFA. The broker never promises a permanent session or asks workers to reopen
sign-in automatically.

## Platform and validation limits

The portable path ceiling is103 encoded bytes, leaving a terminator in Darwin's
104-byte sun_path. Linux provides108 bytes.
[Apple's public header](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/un.h)
defines SOL_LOCAL=0 and LOCAL_PEERPID=2.
[Linux's public header](https://github.com/torvalds/linux/blob/master/include/uapi/linux/un.h)
defines its pathname storage.

Linux uses SO_PEERCRED. Darwin requires getpeereid() and LOCAL_PEERPID. Unsupported
peer identity fails closed; there is no UID-only fallback. Actual platform test
runs govern support claims. A Linux run does not verify Darwin. No local macOS
workstation test or runtime change belongs to this implementation.

Focused allocated tests exercise the real TokenSession, API, BenchmarkClient,
SDK-managed test identity, kernel IPC and four fresh workers. OAuth and HTTP use
synthetic in-memory fixtures; no AWS sign-in occurs. Tests cover rotation,
expiry/revocation, concurrent refresh, fencing, whole-package revalidation,
unsafe peer/path/RPC inputs and credential-free output. Live normal-worker,
SSH and Slurm integration is a later public CLI rollout gate.

## Maintained public controller CLI

The legacy `aws-mesouq-benchmark` flags keep their previous behavior. To retain
authentication across worker replacements, explicitly use `aws-mesouq-benchmark
controller` with the existing normal API/project/operator/journal settings, the
existing guarded SSH state directory, and an approved source-only release package:

```text
aws-mesouq-benchmark controller --api-url HTTPS_API --project PROJECT \
  --oauth-domain EXISTING_ISSUER --client-id EXISTING_CLIENT \
  --operator-config EXISTING_OPERATOR --journal EXISTING_ENROLLED_JOURNAL \
  --ssh-host APPROVED_ALIAS --ssh-state-dir EXISTING_GUARD_DIRECTORY \
  --worker-package PRIVATE_SOURCE_PACKAGE --worker-manifest APPROVED_MANIFEST \
  --worker-binding INDEPENDENTLY_APPROVED_PACKAGE_SHA256
```

Use absolute canonical paths. The controller borrows the existing shared SSH master;
it never creates or retries a physical SSH connection. The separate normal master
must remain available. It does not open the operator/journal/key itself; the source-
pinned managed worker uses the normal SDK to validate them. The public registered
kind is `benchmark-v1`, with entrypoint `aws_mesouq/benchmark_cli.py`. Further queue
implementations require an explicit reviewed registration, journal fencing and fair
queue admission; choosing an arbitrary module through a command is unsupported.

The controller reads bounded JSON lines from its owner's stdin. Supported commands
are `{"operation":"status"}`, `{"operation":"restart"}`,
`{"operation":"stop"}`, or an owner-reviewed package replacement:

```json
{"operation":"use_spec","worker_kind":"benchmark-v1","package_root":"/absolute/private/release","manifest":"/absolute/private/manifest.json","binding":"INDEPENDENTLY_APPROVED_SHA256"}
```

EOF detaches stdin control and leaves the controller running; send SIGTERM/SIGINT to
stop it gracefully. Restart/replacement fences token retrieval, stops only the exact
owned worker, then starts one fresh process. It never cancels Slurm jobs. Journal
reconciliation, ownership checks and reservation settlement remain the existing
worker's responsibility. There is no automatic worker respawn or SSH retry loop.
Startup failure, a fatal worker exit or a transport outage leaves the authenticated
controller alive and paused until the owner requests a guarded restart. A bad package
is validated before stopping a running worker. Status and errors contain fixed codes
and metadata, never arbitrary exception messages, tokens or configuration.

`managed-worker` is an internal supervised mode. It accepts no OAuth flags and has
no login fallback: missing/revoked controller access fails closed. It uses the existing
`BrokerClient.token(force_refresh=...)` API, so normal API401 renewal remains handled
by the same controller TokenSession and unchanged Cognito rules.

The dedicated `Connector Darwin synthetic proof` workflow uses Python3.11 and pinned
checkout/setup-python actions, no AWS secrets, no real OAuth and no SSH. Both Linux
native QA and actual hosted-macOS peer-PID/process proof must pass before activation.
A Linux pass does not qualify Darwin. This source change does not activate or replace
any installed private connector runtime.

Darwin peer identity uses libc getpeereid for effective UID/GID and LOCAL_PEERPID for PID; no Python socket method or UID-only fallback is used. Package leaf opens are nonblocking, so a FIFO cannot hang owner upgrade validation or the child startup gate.

References: https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man3/getpeereid.3.html and https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/un.h .
