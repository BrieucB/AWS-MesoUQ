# Operator setup

The public repository supplies application code. Operators provide an approved
scientific MesoUQ installation, institutional HPC access, approved model hashes,
private account configuration, and an AWS account within the authorized spending
boundary. Deployment and scientific acceptance require private evidence; source
checks do not establish that a live deployment has passed.

## Local connector identity

Install the package in an isolated Python 3.11+ environment on the collaborator's
own macOS or Linux machine. The foreground session uses that collaborator's own
SSH account. Keep application OAuth credentials in memory and the recovery journal
on the local machine. Local execution on an HPC login node requires institutional
approval; it is not the default operating model.

Generate and display the public application-device identity offline:

```sh
aws-mesouq-connector --journal /private/local/aws-mesouq-session --show-archive-public-key
```

Choose an appropriate private local path. The journal directory must be owned by
this user with mode `0700`; its file is `0600`. The command prints the connector
ID, public Ed25519 key, and signature version. It never prints or uploads the
private key and does not connect to AWS or HPC. The `aws-mesouq-connect` command
is an alias for the same CLI.

An authorized operator uses the IAM-controlled identity administration process
to place the public key in the invited identity record:

```json
{
  "connector_public_keys": {
    "CONNECTOR_UUID": "BASE64_RAW_32_BYTE_ED25519_PUBLIC_KEY"
  }
}
```

This is public enrollment material. Keep actual membership/account configuration
private. Browser HTTP cannot register or replace these keys. Follow the service
and infrastructure instructions for the real identity record and storage write;
this example is a shape, not an executable deployment command. Revoke a lost or
compromised device through the same operator-controlled process. A replacement
journal creates a replacement device identity and requires new enrollment.

Both successful-result and archive-verification events need the enrolled device
signature. The version-1 canonical JSON envelope binds job, project, owner,
connector ID, event ID, event type, and the result or receipt; successful results
also bind the recorded Slurm job ID. Lease rotation leaves the event signature
unchanged. This separates the browser credential from the trusted connector's
assertions. It does not provide independent remote attestation of an HPC machine;
the operator trusts the enrolled device and HPC account to perform verification.

## Approve the exact shipped adapter

The trusted model registry must include `approved_adapter_sha256`, independently
approved by the operator. Inspect the shipped standalone adapter, then compute its
SHA-256 from the installed package without importing the scientific engine:

```sh
python -c "from importlib.resources import files; import hashlib; print(hashlib.sha256(files('aws_mesouq').joinpath('adapters/indentation.py').read_bytes()).hexdigest())"
```

On Karolina, invoke `/usr/bin/python3.11` for this command. Computing the hash
identifies the bytes; operator approval is a separate review. Store the approved
hash beside the trusted model descriptor through the existing IAM-controlled
registry process. The connector compares the cloud-approved pin and any private
local registry pin to its shipped adapter before staging or Slurm submission.
The adapter checks its own private snapshot before invoking the existing MesoUQ
engine. A package update that changes the adapter requires fresh review and a
registry pin update; browser input cannot override it.

## Separate SCRATCH and PROJECT roots

At IT4I, computations use SCRATCH. Provision an existing, canonical, account-owned
`0700` compute parent, such as `/scratch/project/PROJECT/user/aws-mesouq`, and pass
it as `--run-root`. Provision a distinct durable PROJECT archive parent, such as
`/mnt/projN/PROJECT/user/aws-mesouq-archive`, also canonical and account-owned
`0700`, and pass it as `--archive-root`. These are generic placeholders. Verify
the actual allocation, storage policy, and required retention with the operator.
The roots cannot be equal, nested, or reached through symlinks.

Provision these dedicated roots without inherited default ACLs. Exact mode `0700`
on a directory alone is insufficient: a default ACL can make files created by
ordinary Python writes group-readable or group-writable despite `umask 077`.
Before staging, and again before Slurm submission, the connector probes both
configured roots using a unique exclusive no-symlink file created with mode
`0666` under `umask 077`. The resulting owned regular file must have exact mode
`0600`. The probe removes only its own matching inode and does not create a job
directory, change ACLs, or alter existing files. A failed probe blocks submission.
An operator must inspect and provision suitable new dedicated private roots;
the connector never repairs or clears ACLs on an existing or shared directory.

Use `--durable-archive-root` only after verifying that separate PROJECT storage
provides the required durability. It does not declare the SCRATCH run root
durable. The connector copies retained inputs, explicitly selected publication
files, and private execution provenance into a private per-job archive. It checks
ownership and symlinks, installs files without overwriting existing bytes,
fsyncs, reopens, and verifies hashes and sizes before a signed receipt. Native
folders are not copied by glob. Execution manifests, runtime scripts, device
private keys, scientific installations, and unselected native files never become
public downloads; the device key remains local rather than entering HPC archives.

Journaled copies and events recover idempotently. Once durable content bindings
exist, already-copied files no longer need their SCRATCH sources, including after
a lost success acknowledgement. Durable files are verified again before an
archive receipt is sent or replayed. Corrupt or inconsistent bytes fail closed.
If the service already accepted an archive event before its acknowledgement was
lost, the connector verifies the authoritative job/receipt and durable bytes and
replays only that exact signed event ID through the service's idempotency path.
It does so without claiming a new lease or acquiring scheduler authority. An
uncommitted receipt follows normal lease recovery. A rotated or revoked device
key cannot authorize a new event through this acknowledgement path.
The service starts the minimum thirty-day AWS retention clock from server receipt
time; database summaries remain retained.

## Site selection and acceptance

Select matching `--site`/`MESOUQ_SITE` as `karolina` or `vega`. Shared interfaces
remain available for both sites. The first release issues Karolina archive
receipts only; live Vega validation remains explicitly deferred. Preserve the
trusted interpreter/runtime and scheduler policies: one node and task, at most
sixteen CPUs and one GPU, no scheduler requeue, and `--export=NIL`.

Before acceptance, complete the private browser-to-AWS-to-Karolina-to-browser
workflow using approved resources and inspect the actual numerical results,
figures, model/adapter provenance, PROJECT archive bytes, signature enforcement,
and recovery behavior. Do not claim acceptance from synthetic tests alone. Keep
real configuration, paths, measurements, models, results, and validation evidence
outside the public export. Consult the service and infrastructure instructions
before any deployment or operator state mutation.
