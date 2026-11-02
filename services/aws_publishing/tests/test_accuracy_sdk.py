"""Synthetic signed SDK-to-controller conformance; no real credentials or compute."""
from copy import deepcopy
import base64
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from test_benchmark import lab
from test_accuracy import accuracy_lab, call, spec
from aws_mesouq.benchmark_accuracy_client import AccuracyClient
from aws_mesouq.connector.api import APIError as ConnectorAPIError
from aws_mesouq.benchmark_accuracy_contexts import (
    AccuracyExecutionContexts, REGISTRY_SCHEMA, read_registry)
from aws_mesouq import benchmark_accuracy_protocol as p


def registry(suite):
    operator = {
        "repository": "/hpc/science", "python_bin": "/hpc/env/bin/python",
        "adapter_path": "/hpc/site/aws_mesouq/adapters/benchmark_accuracy.py",
        "environment_script": "/hpc/environment.sh", "environment_sha256": "f"*64,
        "expected_host": "login1.karolina.it4i.cz",
        "scheduler": {"partition": "qgpu", "account": "approved-account", "qos": "approved-qos", "mem": "64G"},
        "allowed_upload_origins": ["https://private-bucket.s3.eu-central-1.amazonaws.com"]}
    context = deepcopy(suite[5])
    operator.update(python_sha256="1"*64, dependency_manifest_path="/hpc/dependencies.json",
        dependency_manifest_sha256="2"*64)
    own = ["__init__.py", "benchmark.py", "benchmark_accuracy.py",
        "benchmark_accuracy_protocol.py", "adapters/__init__.py",
        "adapters/benchmark.py", "adapters/benchmark_accuracy.py"]
    sources = {"/hpc/site/aws_mesouq/" + name: "3"*64 for name in own}
    sources[operator["adapter_path"]] = context["adapter_sha256"]
    for name in ("__init__.py", "benchmark_lab/__init__.py",
                 "benchmark_lab/accuracy_v2/__init__.py"):
        sources["/hpc/science/src/meso_uq/" + name] = "4"*64
    operator["native_python_sources"] = sources
    context["runtime_profile_sha256"] = p.checksum(operator)
    context["id"] = p.context_id(context)
    suite[0].accuracy_policies["p"] = [context]
    document = {"schema": REGISTRY_SCHEMA, "contexts": [{"context": context, "operator": operator}]}
    return document, context


def sdk(suite, monkeypatch):
    class API:
        def __init__(self):
            self.sent = []
        def request(self, method, path, payload=None):
            self.sent.append((method, path, deepcopy(payload)))
            url = urlsplit(path)
            if url.query:
                assert payload is None
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                if "limit" in query:
                    query["limit"] = int(query["limit"])
                payload = query
            status, response = call(suite, method, url.path, payload)
            if status >= 400:
                error = response.get("error", response)
                raise ConnectorAPIError(status, error.get("code", "synthetic_error"),
                    error.get("message", str(response)))
            return response
    api = API()
    client = AccuracyClient(api, object(), "alice")
    monkeypatch.setattr(client, "_identity", lambda: {"connector_id": "c1"})
    monkeypatch.setattr(client, "_sign", lambda value:
        base64.b64encode(suite[4].sign(value)).decode())
    return client, api


def test_frozen_signed_client_replays_exact_claim_and_event_against_controller(accuracy_lab, monkeypatch):
    document, context = registry(accuracy_lab)
    contexts = AccuracyExecutionContexts(document, project="p", owner_sub="alice")
    client, api = sdk(accuracy_lab, monkeypatch)
    created = client.create("p", spec("opaque \n nonce Ω"))["campaign"]
    body = client.build_claim_body("p", "request-one", supported_contexts=contexts.supported(),
                                   campaign_id=created["id"])
    before = deepcopy(body)
    claimed = contexts.validate_claim(client.claim_frozen(body))
    replay = client.claim_frozen(body)
    assert claimed == replay and body == before and "signature" not in body
    assert claimed["task"]["manifest"]["request"]["idempotency_key"] == "opaque \n nonce Ω"
    event = client.build_event_body("p", claimed, "event-one", "heartbeat", {})
    frozen = deepcopy(event)
    first = client.event_frozen(event)
    assert client.event_frozen(event) == first and event == frozen
    assert event["expected_task_revision"] == claimed["task"]["revision"]
    assert p.event_signing_bytes("alice", {**event, "signature": "ignored"}) == p.event_signing_bytes("alice", event)
    assert "signature" not in event
    assert client.tasks("p", created["id"], limit=1)["tasks"]
    assert client.accounting("p", created["id"])["snapshot"]["campaign_instance_id"] == created["id"]


@pytest.mark.parametrize("change", ["context", "source", "manifest", "request", "lease"])
def test_context_rejects_transplanted_claim_identity(accuracy_lab, monkeypatch, change):
    document, context = registry(accuracy_lab)
    contexts = AccuracyExecutionContexts(document, project="p", owner_sub="alice")
    client, _ = sdk(accuracy_lab, monkeypatch)
    cid = client.create("p", spec())["campaign"]["id"]
    claimed = client.claim_frozen(client.build_claim_body("p", "one",
        supported_contexts=contexts.supported(), campaign_id=cid))
    if change == "context":
        claimed["campaign"]["execution_context_id"] = "0"*64
    elif change == "source":
        claimed["task"]["manifest"]["source_binding_sha256"] = "0"*64
        claimed["task"]["manifest_sha256"] = p.checksum(claimed["task"]["manifest"])
    elif change == "manifest":
        claimed["task"]["manifest_sha256"] = "0"*64
    elif change == "request":
        claimed["campaign"]["configuration"]["seed"] += 1
    else:
        claimed["task"]["lease_epoch"] += 1
    with pytest.raises(ValueError):
        contexts.validate_claim(claimed)


@pytest.mark.parametrize("mutation", ["owner", "project", "profile", "host", "scheduler", "path", "origin", "duplicate"])
def test_explicit_registry_rejects_unreviewed_execution_changes(accuracy_lab, mutation):
    document, context = registry(accuracy_lab)
    entry = document["contexts"][0]
    if mutation == "owner":
        entry["context"]["owner_sub"] = "bob"
        entry["context"]["id"] = p.context_id(entry["context"])
    elif mutation == "project":
        entry["context"]["project_id"] = "other"
        entry["context"]["id"] = p.context_id(entry["context"])
    elif mutation == "profile":
        entry["operator"]["scheduler"]["account"] = "changed"
    elif mutation == "host":
        entry["operator"]["expected_host"] = "unapproved.example"
    elif mutation == "scheduler":
        entry["operator"]["scheduler"]["qos"] = "x\n#SBATCH --gpus=8"
    elif mutation == "path":
        entry["operator"]["repository"] = "/hpc/../arbitrary"
    elif mutation == "origin":
        entry["operator"]["allowed_upload_origins"] = ["https://private-bucket.example/path"]
    else:
        document["contexts"].append(deepcopy(entry))
    with pytest.raises(ValueError):
        AccuracyExecutionContexts(document, project="p", owner_sub="alice")


def test_registry_file_duplicate_fields_permissions_symlink_and_nonfinite_rejected(tmp_path):
    path = tmp_path / "accuracy.json"
    path.write_text('{"schema":"x","schema":"y"}')
    path.chmod(0o600)
    with pytest.raises(ValueError, match="Duplicate"):
        read_registry(path)
    path.write_text('{"value":NaN}')
    with pytest.raises(ValueError, match="Finite"):
        read_registry(path)
    path.write_text('{}')
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        read_registry(path)
    path.chmod(0o600)
    link = tmp_path / "alias.json"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="direct"):
        read_registry(link)


def test_client_cannot_change_enrolled_connector_or_resign_signed_bodies(accuracy_lab, monkeypatch):
    document, context = registry(accuracy_lab)
    client, api = sdk(accuracy_lab, monkeypatch)
    body = client.build_claim_body("p", "one", supported_contexts=[context["id"]])
    body["connector_id"] = "other"
    with pytest.raises(ValueError, match="enrolled"):
        client.claim_frozen(body)
    body["connector_id"] = "c1"
    body["signature"] = "not-an-unsigned-body"
    with pytest.raises(ValueError, match="unsigned"):
        client.claim_frozen(body)
    assert api.sent == []


def test_exact_task_renewal_never_claims_another_earlier_ready_task(accuracy_lab, monkeypatch):
    document, context = registry(accuracy_lab)
    contexts = AccuracyExecutionContexts(document, project="p", owner_sub="alice")
    client, api = sdk(accuracy_lab, monkeypatch)
    cid = client.create("p", spec())["campaign"]["id"]
    original = client.claim_frozen(client.build_claim_body("p", "initial",
        supported_contexts=contexts.supported(), campaign_id=cid))
    target = original["task"]["id"]
    state = accuracy_lab[1].state
    campaign = state["accuracy_campaigns"]["p:alice:" + cid]
    earlier = deepcopy(state["accuracy_tasks"][cid + ":" + target])
    earlier.update(id="t00000000-earlier", status="ready", lease=None, lease_epoch=0, revision=0)
    earlier["manifest"]["task_id"] = earlier["id"]
    earlier["manifest_sha256"] = p.checksum(earlier["manifest"])
    state["accuracy_tasks"][cid + ":" + earlier["id"]] = earlier
    campaign["active_tasks"].insert(0, earlier["id"])
    body = client.build_claim_body("p", "renew-exact", supported_contexts=contexts.supported(),
                                  campaign_id=cid, task_id=target)
    renewed = client.claim_frozen(body)
    assert contexts.validate_claim(renewed)["task"]["id"] == target
    assert renewed["task"]["lease"] == original["task"]["lease"]
    assert state["accuracy_tasks"][cid + ":" + earlier["id"]]["lease"] is None
    assert client.claim_frozen(body) == renewed
    changed = deepcopy(body)
    changed["task_id"] = earlier["id"]
    with pytest.raises(ConnectorAPIError) as rejected:
        client.claim_frozen(changed)
    assert rejected.value.code == "idempotency_conflict"
    wrong = client.build_claim_body("p", "wrong-context", supported_contexts=["0"*64],
                                   campaign_id=cid, task_id=target)
    assert client.claim_frozen(wrong) == {"campaign": None, "task": None}
    other = client.create("p", spec("other-campaign"))["campaign"]["id"]
    wrong = client.build_claim_body("p", "wrong-campaign", supported_contexts=contexts.supported(),
                                   campaign_id=other, task_id=target)
    assert client.claim_frozen(wrong) == {"campaign": None, "task": None}
    accuracy_lab[1].state["accuracy_tasks"][cid + ":" + target]["lease"]["connector_id"] = "foreign"
    foreign = client.build_claim_body("p", "foreign-live", supported_contexts=contexts.supported(),
                                     campaign_id=cid, task_id=target)
    assert client.claim_frozen(foreign) == {"campaign": None, "task": None}


def test_exact_target_requires_campaign_and_conflict_free_filters(accuracy_lab, monkeypatch):
    document, context = registry(accuracy_lab)
    client, api = sdk(accuracy_lab, monkeypatch)
    with pytest.raises(ValueError, match="requires its campaign"):
        client.build_claim_body("p", "invalid", supported_contexts=[context["id"]], task_id="task")
    with pytest.raises(ValueError, match="conflicts"):
        client.build_claim_body("p", "invalid", supported_contexts=[context["id"]],
                                campaign_id="campaign", task_id="task", exclude_task_ids=["task"])
    assert not api.sent


def test_recovering_persisted_cloud_submission_intent_is_reconcile_only(accuracy_lab, monkeypatch):
    document, context = registry(accuracy_lab)
    contexts = AccuracyExecutionContexts(document, project="p", owner_sub="alice")
    client, api = sdk(accuracy_lab, monkeypatch)
    cid = client.create("p", spec())["campaign"]["id"]
    claim = client.claim_frozen(client.build_claim_body("p", "initial-intent",
        supported_contexts=contexts.supported(), campaign_id=cid))
    intent = {"script_sha256": "a"*64, "intent_sha256": "b"*64, "marker": claim["task"]["marker"]}
    client.event_frozen(client.build_event_body("p", claim, "persist-intent", "submitting", intent))
    recovered = client.claim_frozen(client.build_claim_body("p", "recover-intent",
        supported_contexts=contexts.supported(), campaign_id=cid, task_id=claim["task"]["id"]))
    assert recovered["task"]["status"] == "submitting"
    assert recovered["task"]["reconcile_only"] is True
    assert recovered["task"]["manifest_sha256"] == claim["task"]["manifest_sha256"]
    assert recovered["task"]["slurm_job_id"] is None


@pytest.mark.parametrize("overlays", [
    "/hpc/deps/site", ["/hpc/deps/site"]*2, ["/hpc/site"], ["/hpc/site/aws_mesouq"],
    ["/hpc/science/src"], ["/hpc/science/src/meso_uq"], ["/hpc"],
    ["/hpc/deps/../site"], ["/hpc/deps//site"], ["/hpc/deps/site/"],
    ["/hpc/deps/site", "/hpc/deps/site/child"], ["relative/site"],
    ["/hpc/deps/$ambient"], [None], ["/hpc/deps/site"+str(i) for i in range(9)],
])
def test_dependency_overlays_are_bounded_canonical_and_cannot_supply_own_packages(accuracy_lab, overlays):
    document, context = registry(accuracy_lab)
    operator = document["contexts"][0]["operator"]
    operator["dependency_overlays"] = overlays
    context["runtime_profile_sha256"] = p.checksum(operator)
    context["id"] = p.context_id(context)
    with pytest.raises(ValueError, match="overlay"):
        AccuracyExecutionContexts(document, project="p", owner_sub="alice")


def test_dependency_overlay_order_and_presence_are_runtime_profile_bound(accuracy_lab):
    document, context = registry(accuracy_lab)
    operator = document["contexts"][0]["operator"]
    operator["dependency_overlays"] = ["/hpc/deps/jax/site", "/hpc/deps/korali/site"]
    context["runtime_profile_sha256"] = p.checksum(operator)
    context["id"] = p.context_id(context)
    contexts = AccuracyExecutionContexts(document, project="p", owner_sub="alice")
    assert contexts.operator(context["id"])["dependency_overlays"] == operator["dependency_overlays"]
    operator["dependency_overlays"].reverse()
    with pytest.raises(ValueError, match="profile"):
        AccuracyExecutionContexts(document, project="p", owner_sub="alice")
