"""Signed, durable-body client for the separate accuracy-v2 task protocol.

The caller persists unsigned bodies before invoking any *_frozen method.  Signing
reuses an enrolled journal; this module never enrolls or creates an identity.
"""
from __future__ import annotations

from copy import deepcopy
from urllib.parse import urlencode

from . import benchmark_accuracy as wire
from . import benchmark_accuracy_protocol as protocol


class AccuracyClient:
    def __init__(self, api, journal, owner_sub):
        self.api, self.journal = api, journal
        self.owner_sub = protocol.identifier(owner_sub, "owner subject")

    def _identity(self):
        from .benchmark_worker import existing_identity
        return existing_identity(self.journal)

    def _sign(self, message):
        from .connector.signing import _sign
        return _sign(self.journal, message)

    @staticmethod
    def _base(project):
        return f"/v1/projects/{protocol.identifier(project)}/benchmark-accuracy"

    def capabilities(self, project):
        return self.api.request("GET", self._base(project) + "/capabilities")

    def create(self, project, specification):
        return self.api.request("POST", self._base(project) + "/campaigns",
                                wire.validate_create(specification))

    def campaigns(self, project, *, cursor=None, limit=50, archived="all"):
        protocol.integer(limit, "page limit", 1, 50)
        if archived not in ("all", "true", "false"):
            raise ValueError("Invalid archived filter")
        query = {"limit": limit, "archived": archived}
        if cursor is not None:
            if not isinstance(cursor, str) or len(cursor) > 4096:
                raise ValueError("Invalid cursor")
            query["cursor"] = cursor
        return self.api.request("GET", self._base(project) + "/campaigns?" + urlencode(query))

    def get(self, project, campaign):
        return self.api.request("GET", self._base(project) + "/campaigns/" +
                                protocol.identifier(campaign))

    def tasks(self, project, campaign, *, cursor=None, limit=50):
        protocol.integer(limit, "page limit", 1, 100)
        query = {"limit": limit}
        if cursor is not None:
            if not isinstance(cursor, str) or len(cursor) > 4096:
                raise ValueError("Invalid cursor")
            query["cursor"] = cursor
        return self.api.request("GET", self._base(project) + "/campaigns/" +
            protocol.identifier(campaign) + "/tasks?" + urlencode(query))

    def task(self, project, campaign, task):
        return self.api.request("GET", self._base(project) + "/campaigns/" +
            protocol.identifier(campaign) + "/tasks/" + protocol.identifier(task))

    def accounting(self, project, campaign):
        response = self.api.request("GET", self._base(project) + "/campaigns/" +
                                    protocol.identifier(campaign) + "/accounting")
        protocol.keys(response, {"snapshot", "sha256"})
        protocol.validate_snapshot(response["snapshot"],
            allow_unprepared=response["snapshot"]["case_plan_sha256"] is None)
        if protocol.checksum(response["snapshot"]) != response["sha256"]:
            raise ValueError("Authoritative accounting checksum mismatch")
        return response

    def build_claim_body(self, project, request_id, *, supported_contexts,
                         campaign_id=None, task_id=None, exclude_task_ids=None):
        body = {"project_id": project, "connector_id": self._identity()["connector_id"],
                "site": "karolina", "request_id": request_id,
                "supported_contexts": deepcopy(supported_contexts)}
        if campaign_id is not None:
            body["campaign_id"] = campaign_id
        if task_id is not None:
            body["task_id"] = task_id
        if exclude_task_ids is not None:
            body["exclude_task_ids"] = deepcopy(exclude_task_ids)
        return self._claim_body(body)

    def _claim_body(self, body):
        if "signature" in body:
            raise ValueError("Persist the unsigned claim body")
        result = protocol.validate_claim(body)
        if result["connector_id"] != self._identity()["connector_id"]:
            raise ValueError("Frozen claim differs from the existing enrolled identity")
        return result

    def claim_frozen(self, body):
        payload = self._claim_body(body)
        payload["signature"] = self._sign(protocol.claim_signing_bytes(self.owner_sub, payload))
        return self.api.request("POST", "/v1/benchmark-connector/accuracy/claim", payload)

    def build_event_body(self, project, claim, event_id, kind, payload):
        campaign, task = claim["campaign"], claim["task"]
        body = {"project_id": project, "connector_id": self._identity()["connector_id"],
            "campaign_id": campaign["id"], "task_id": task["id"],
            "attempt": task["attempt"], "lease_epoch": task["lease"]["epoch"],
            "lease_token": task["lease"]["token"], "event_id": event_id,
            "expected_task_revision": task["revision"],
            "manifest_sha256": task["manifest_sha256"], "type": kind,
            "payload": deepcopy(payload)}
        return self._event_body(body)

    def _event_body(self, body):
        if "signature" in body:
            raise ValueError("Persist the unsigned event body")
        result = protocol.validate_event({**deepcopy(body), "signature": ""})
        del result["signature"]
        if result["connector_id"] != self._identity()["connector_id"]:
            raise ValueError("Frozen event differs from the existing enrolled identity")
        return result

    def event_frozen(self, body):
        payload = self._event_body(body)
        payload["signature"] = self._sign(protocol.event_signing_bytes(self.owner_sub, payload))
        path = ("/v1/benchmark-connector/accuracy/campaigns/" + payload["campaign_id"] +
                "/tasks/" + payload["task_id"] + "/events")
        return self.api.request("POST", path, payload)

    def build_archive_body(self, project, campaign, event_id, receipt):
        body = {"project_id": protocol.identifier(project),
            "campaign_id": protocol.identifier(campaign),
            "connector_id": self._identity()["connector_id"],
            "event_id": protocol.identifier(event_id), "receipt": deepcopy(receipt)}
        protocol.canonical(body)
        return body

    def archive_frozen(self, body):
        protocol.keys(body, {"project_id", "campaign_id", "connector_id", "event_id", "receipt"})
        for field in ("project_id", "campaign_id", "connector_id", "event_id"):
            protocol.identifier(body[field], field)
        if body["connector_id"] != self._identity()["connector_id"]:
            raise ValueError("Frozen archive differs from the existing enrolled identity")
        payload = deepcopy(body)
        payload["signature"] = self._sign(protocol.archive_signing_bytes(self.owner_sub, payload))
        return self.api.request("POST", "/v1/benchmark-connector/accuracy/campaigns/" +
                                payload["campaign_id"] + "/archive", payload)

    def cancel(self, project, campaign):
        return self.api.request("POST", self._base(project) + "/campaigns/" +
                                protocol.identifier(campaign) + "/cancel", {})

    def request_archive(self, project, campaign):
        return self.api.request("POST", self._base(project) + "/campaigns/" +
                                protocol.identifier(campaign) + "/archive", {})

    def results(self, project, campaign):
        return self.api.request("GET", self._base(project) + "/campaigns/" +
                                protocol.identifier(campaign) + "/results")
