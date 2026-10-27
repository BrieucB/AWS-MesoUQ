"""Local application-device identity for operator-enrolled archive attestations.

This key is neither an HPC credential nor an AWS credential. Only its public
half is enrolled by an operator through IAM; browser HTTP cannot enroll keys.
"""
from __future__ import annotations

import base64
import json
import uuid


def archive_signing_bytes(job_id: str, owner_sub: str, payload: dict) -> bytes:
    if payload["type"] != "archive_verified":
        raise ValueError("Expected an archive verification event")
    return _signing_bytes(job_id, owner_sub, payload, "archive")


def result_signing_bytes(job_id: str, owner_sub: str, payload: dict) -> bytes:
    if payload["type"] != "succeeded":
        raise ValueError("Expected a successful scientific result event")
    return _signing_bytes(job_id, owner_sub, payload, "result")


def _signing_bytes(job_id, owner_sub, payload, content_key):
    value = {"version": 1, "job_id": job_id, "project_id": payload["project_id"],
             "owner_sub": owner_sub, "connector_id": payload["connector_id"],
             "event_id": payload["event_id"], "type": payload["type"], content_key: payload[content_key]}
    if content_key == "result":
        value["slurm_job_id"] = payload.get("slurm_job_id")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def device_identity(journal) -> dict:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, PublicFormat, NoEncryption
    changed = False
    if "connector_id" not in journal.data:
        journal.data["connector_id"] = str(uuid.uuid4())
        changed = True
    device = journal.data.get("archive_device")
    if device is None:
        key = Ed25519PrivateKey.generate()
        device = {"version": 1, "connector_id": journal.data["connector_id"],
                  "private_key": base64.b64encode(key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())).decode("ascii"),
                  "public_key": base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode("ascii")}
        journal.data["archive_device"] = device
        changed = True
    if set(device) != {"version", "connector_id", "private_key", "public_key"} or device["version"] != 1 or device["connector_id"] != journal.data["connector_id"]:
        raise ValueError("Invalid private archive device identity binding")
    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(device["private_key"], validate=True))
    public_key = base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode("ascii")
    if public_key != device["public_key"]:
        raise ValueError("Private archive device key differs from its enrolled public identity")
    if changed:
        journal.save()
    return {"connector_id": journal.data["connector_id"], "archive_public_key": public_key,
            "archive_signature_version": 1}


def sign_archive(journal, job_id: str, owner_sub: str, payload: dict) -> str:
    return _sign(journal, archive_signing_bytes(job_id, owner_sub, payload))


def sign_result(journal, job_id: str, owner_sub: str, payload: dict) -> str:
    return _sign(journal, result_signing_bytes(job_id, owner_sub, payload))


def _sign(journal, message):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    device_identity(journal)
    device = journal.data["archive_device"]
    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(device["private_key"], validate=True))
    return base64.b64encode(key.sign(message)).decode("ascii")
