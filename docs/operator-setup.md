# Operator setup

The public repository supplies application code. Operators provide an approved
scientific MesoUQ installation, institutional HPC access, approved model hashes,
private account configuration, and an AWS account within the authorized spending
boundary. Deployment and scientific acceptance require private evidence; source
checks do not establish that a live deployment has passed.

## Publish the backend image

Live operations require owner authorization for their account, resource changes,
and spending boundary. Existing authorization for this project applies within
that agreed scope. Offline validation remains offline; the publication workflow
does not deploy Lambda, apply Terraform, administer IAM, invite users, or change
an account plan. Website publication uses the separate manual static workflow described below.

An operator provisions the private ECR repository with immutable tags and no
mutability exclusions, plus an ECR-only OIDC publication role. Configure the
GitHub environment `aws-publishing` to allow deployments from `main` only, and
scope the role trust to this exact repository and environment. Verify the actual
GitHub OIDC subject format when setting the trust policy; do not broaden it to
wildcard repositories, environments, or branches. Configure these four values as
environment **secrets**, including values that are not AWS credentials:

| Secret | Private value |
| --- | --- |
| `AWS_PUBLISH_ROLE_ARN` | The narrowly scoped OIDC role ARN |
| `AWS_REGION` | The repository's AWS Region |
| `AWS_ECR_REPOSITORY_NAME` | The existing private repository name |
| `AWS_ECR_REPOSITORY_URL` | Its registry/repository URI, without a tag or scheme |

In a private operator session, read the repository's current OIDC configuration:

```sh
gh api repos/OWNER/REPOSITORY/actions/oidc/customization/sub --jq '{use_immutable_subject, sub_claim_prefix}'
```

Use the returned `sub_claim_prefix` unchanged as the private Terraform input
`github_oidc_subject_prefix`; the trust policy appends exactly
`:environment:aws-publishing`. Preserve immutable subjects when enabled. Do not
opt out, substitute a guessed legacy prefix, or add a wildcard to compensate for
a subject mismatch. Keep the resulting account/trust configuration private.

Run **Publish backend image** manually from `main` after its required source
checks pass. The job also checks `refs/heads/main`, uses the protected environment,
and checks out the exact dispatch commit. The AWS credential action is pinned to
the reviewed official release commit and obtains a short-lived OIDC session;
long-lived AWS keys are not configured. ECR login state is temporary and removed
after the image operation.

The workflow publishes only `source-<full Git commit SHA>` in the configured
repository. It builds a minimal context from selected tracked control-plane,
contract, and preset files, excluding measurements, models, connector journals,
deployment configuration, and the HPC engine. The image targets `linux/amd64`
with one OCI image manifest, with provenance and SBOM attestations disabled to
avoid a Lambda-incompatible image index. It verifies the registry manifest digest,
image configuration, architecture, and OCI source/revision labels.

If that immutable source tag already exists, the job verifies its exact source
labels, digest, and platform and succeeds without rebuilding or overwriting it.
A mutable repository, mismatched image, unsupported manifest, or uncertain AWS
response fails closed. Base images and dependencies can change between builds;
rerunning a source commit preserves its already published verified image.

Account, role, repository, registry, image URI, and discovered digests remain
masked. AWS/Docker output is captured rather than streamed, and the workflow
uploads no build records, artifacts, deployment outputs, or account configuration.
Keep debug logging disabled. A failure reports only its operation stage; inspect
any required destination details through authorized private operator tooling.
Obtain the verified immutable `@sha256:...` image reference privately for a
separate reviewed Terraform plan. Never paste it into a public issue, workflow
output, source file, or pull-request comment.

These choices follow the official
[AWS credential action](https://github.com/aws-actions/configure-aws-credentials),
[Lambda image requirements](https://docs.aws.amazon.com/lambda/latest/dg/images-create.html),
and [ECR tag immutability guidance](https://docs.aws.amazon.com/AmazonECR/latest/userguide/image-tag-mutability.html).


## Publish the inline posterior website update

Use **Publish website static delta** only after the reviewed source change is
merged, required checks pass, and the operator has approved this static release.
Dispatch `publish-website.yml` manually on `main` in the existing
`aws-publishing` environment. Supply the exact reviewed main SHA as
`expected_source_commit`, the exact source SHA matching the currently deployed
baseline as `baseline_commit` (initial default:
`457c2baa2860d3b144ac5e045e98c2769c69f21a`), and the existing scoped publication
role ARN and website bucket as private inputs. Check both commits and destination
privately before approving the environment deployment. Keep debug logging
disabled and do not copy private inputs into issues, PRs or release notes.

The first step reads dispatch input values from the event file and masks role,
account and bucket before exporting them. Authentication executes the unchanged
official AWS credential client bundle from its exact reviewed commit under Node
24. A bounded wrapper forwards only complete standard log-mask commands from
its captured stdout/stderr; other authentication output is discarded and failures
report a generic category. No authentication log files are created. The normal
OIDC client exports short-lived credentials through the runner environment and
its official cleanup bundle clears them even after publication failure. Owner
credentials and long-lived AWS keys are not used. The job checks exact source identity
and baseline ancestry, then builds both public frontend trees under runner
temporary storage. It uses the existing repository/environment OIDC trust and
`AWS_REGION` secret, with a short session policy allowing reads of exactly the ten website assets and
writes to only `src/posterior.js`, `src/style.css` and `src/app.js`. It does not
change IAM, environment settings, bucket configuration or permissions. The
session also retains ListBucket on exactly the selected bucket, so a missing
posterior object yields a distinguishable 404 rather than AccessDenied.
Destination and object metadata are never logged.

The helper rejects root, IAM user and unexpected assumed-role identities before
accessing S3. It checks all nine existing assets against the reviewed baseline
(or exact target bytes on a partial rerun), preserves the live
`runtime-config.json` byte for byte, and adds the posterior module first,
replaces CSS second, and replaces the application entry module last.
Writes use ETag/absence conditions, SHA256 checksums, AES256 encryption and
`no-store`; readback verifies all ten assets. Unexpected live bytes or metadata
fail closed. A verified rerun skips already published target bytes. Failure after
an earlier write can leave a partial static update; inspect the sanitized failure
category privately and rerun only the same reviewed source/baseline after
resolving the cause. There is no automatic rollback or deletion.

No artifacts or private reports are uploaded. The workflow prints only sanitized
verification counts and a manifest digest. CloudFront TTL is zero, so no
invalidation is requested. This release does not dispatch the backend image
workflow, deploy backend services, run scientific jobs, or require a new HPC
calculation to display an existing completed posterior figure.

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
