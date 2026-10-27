"""Verify operator-enrolled device attestations without trusting browser claims."""

import base64
import json
from pathlib import PurePosixPath

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def archive_payload(job, event):
    """Canonical v1 envelope shared with the private connector signer."""
    return json.dumps({
        "version": 1,
        "job_id": job["id"],
        "project_id": event["project_id"],
        "owner_sub": job["owner_sub"],
        "connector_id": event["connector_id"],
        "event_id": event["event_id"],
        "type": "archive_verified",
        "archive": event["archive"],
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def result_payload(job, event):
    return json.dumps({
        "version": 1, "job_id": job["id"], "project_id": event["project_id"],
        "owner_sub": job["owner_sub"], "connector_id": event["connector_id"],
        "event_id": event["event_id"], "type": "succeeded", "result": event["result"],
        "slurm_job_id": event.get("slurm_job_id"),
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def verify_signature(job, identity, event, *, field, payload):
    keys = identity.get("connector_public_keys", {})
    key = keys.get(event["connector_id"]) if isinstance(keys, dict) else None
    signature = event.get(field)
    if not isinstance(key, str) or not isinstance(signature, str):
        raise ValueError("An operator-enrolled connector signature is required")
    try:
        public_bytes = base64.b64decode(key, validate=True)
        signature_bytes = base64.b64decode(signature, validate=True)
        if len(public_bytes) != 32 or len(signature_bytes) != 64:
            raise ValueError("Invalid Ed25519 signature encoding")
        Ed25519PublicKey.from_public_bytes(public_bytes).verify(signature_bytes, payload(job, event))
    except (ValueError, TypeError, KeyError, InvalidSignature) as exc:
        raise ValueError("Invalid operator-enrolled connector signature") from exc


def verify_result(job, identity, event):
    allowed = {"project_id", "connector_id", "lease_token", "event_id", "type", "slurm_job_id", "result", "result_signature"}
    if set(event) - allowed:
        raise ValueError("Unexpected fields in signed completion")
    verify_signature(job, identity, event, field="result_signature", payload=result_payload)


def verify_archive(job, identity, event):
    """Require a successful Karolina lease and its registered device signature.

    The signature attests to the enrolled connector's verification; an absolute
    path and signature are not independent evidence of HPC filesystem durability.
    """
    allowed = {"project_id", "connector_id", "lease_token", "event_id", "type", "archive", "archive_signature"}
    if set(event) - allowed:
        raise ValueError("Unexpected fields in signed archive receipt")
    receipt = event.get("archive")
    lease = job.get("lease", {})
    if (
        job.get("status") != "succeeded"
        or job.get("site") != "karolina"
        or lease.get("archive_capable") is not True
        or not isinstance(receipt, dict)
        or receipt.get("site") != job.get("site")
    ):
        raise ValueError("Archive requires a successful Karolina job and archive-capable lease")
    path = receipt.get("path")
    if (
        not isinstance(path, str) or not path.startswith("/") or path.startswith("//")
        or len(path) > 2048 or any(ord(c) < 32 for c in path)
        or str(PurePosixPath(path)) != path or ".." in PurePosixPath(path).parts
        or path == "/"
    ):
        raise ValueError("Archive requires a canonical absolute private path")
    verify_signature(job, identity, event, field="archive_signature", payload=archive_payload)
