"""Private connector protocol for Benchmark Lab; no scheduler or science imports."""
from __future__ import annotations

from .benchmark import claim_signing_bytes, stage_signing_bytes, archive_signing_bytes


class BenchmarkClient:
    """Reuse the caller's normal authenticated API and operator-enrolled device.

    The journal is supplied by the connector. This class never discovers credentials,
    enrolls a new key through HTTP, or invokes a scheduler.
    """
    def __init__(self, api, journal, owner_sub):
        self.api, self.journal, self.owner_sub = api, journal, owner_sub

    def _identity(self):
        from .connector.signing import device_identity
        return device_identity(self.journal)

    def _sign(self, message):
        from .connector.signing import _sign
        return _sign(self.journal, message)

    def capabilities(self, project):
        return self.api.request("GET", f"/v1/projects/{project}/benchmarks/capabilities")

    def create(self, project, specification):
        return self.api.request("POST", f"/v1/projects/{project}/benchmarks", specification)

    def get(self, project, campaign):
        return self.api.request("GET", f"/v1/projects/{project}/benchmarks/{campaign}")

    def claim(self, project, request_id, *, campaign_id=None, exclude_stage_ids=None):
        identity = self._identity()
        body = {"project_id": project, "connector_id": identity["connector_id"], "site": "karolina", "request_id": request_id}
        if campaign_id is not None:
            body["campaign_id"] = campaign_id
        if exclude_stage_ids is not None:
            body["exclude_stage_ids"] = exclude_stage_ids
        body["signature"] = self._sign(claim_signing_bytes(self.owner_sub, body))
        return self.api.request("POST", "/v1/benchmark-connector/claim", body)

    def event(self, claim, event_id, kind, **fields):
        campaign, stage = claim["campaign"], claim["stage"]
        body = {"project_id": campaign["project_id"], "connector_id": self._identity()["connector_id"], "lease_token": claim["lease_token"], "event_id": event_id, "type": kind, **fields}
        body["signature"] = self._sign(stage_signing_bytes(campaign, stage, body))
        action = "artifacts" if kind == "artifact_upload" else "events"
        return self.api.request("POST", f"/v1/benchmark-connector/campaigns/{campaign['id']}/stages/{stage['id']}/{action}", body)

    def upload_artifacts(self, claim, event_id, artifacts):
        """Reserve private uploads, then caller transfers only to returned signed URLs.

        artifacts is [{name,sha256,size_bytes,media_type}]. Completion references add
        the returned id; successful stage events verify object checksum and size.
        """
        return self.event(claim, event_id, "artifact_upload", artifacts=artifacts)

    def cancel(self, project, campaign):
        return self.api.request("POST", f"/v1/projects/{project}/benchmarks/{campaign}/cancel", {})

    def results(self, project, campaign):
        return self.api.request("GET", f"/v1/projects/{project}/benchmarks/{campaign}/results")

    def request_archive(self, project, campaign):
        return self.api.request("POST", f"/v1/projects/{project}/benchmarks/{campaign}/archive", {})

    def archive(self, campaign, event_id, receipt):
        body = {"project_id": campaign["project_id"], "campaign_id": campaign["id"], "connector_id": self._identity()["connector_id"], "event_id": event_id, "type": "archive_verified", "receipt": receipt}
        body["signature"] = self._sign(archive_signing_bytes(campaign, body))
        return self.api.request("POST", "/v1/benchmark-connector/archive", body)


def native_accounting(*, job_id, state, elapsed_seconds, allocated_gpus, cpu_seconds):
    """Use actual sacct AllocTRES/ElapsedRaw/CPUTimeRAW, never requested resources."""
    return {"source": "sacct", "site": "karolina", "slurm_job_id": str(job_id), "state": state, "elapsed_seconds": elapsed_seconds, "allocated_gpus": allocated_gpus, "cpu_seconds": cpu_seconds}
