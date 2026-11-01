"""Dynamo request round trips and deployed accuracy event byte boundaries.

All keys, requests and source receipts here are synthetic. No AWS/science runs.
"""
from copy import deepcopy
import base64
import io
import json
from urllib.parse import urlsplit

import pytest

from aws_mesouq import benchmark_accuracy_protocol as p
from aws_mesouq.connector.api import API
from aws_publishing import handler
from aws_publishing.accuracy import AccuracyApplication
from aws_publishing.api import APIError, Application
from aws_publishing.storage import DynamoStore, empty
from test_benchmark import lab
from test_accuracy import (
    accuracy_lab, call, claim_accuracy, create_accuracy, event_body, send_event,
)
import test_accuracy_graph as graph


class FakeDynamo:
    """Persist raw low-level Dynamo records; each read returns independent bytes."""
    class exceptions:
        class TransactionCanceledException(Exception):
            def __init__(self):
                self.response = {"CancellationReasons": [{"Code": "ConditionalCheckFailed"}]}

    def __init__(self, state):
        self.items = {
            bucket + ":" + key: {"pk": {"S": "STATE"}, "sk": {"S": bucket + ":" + key},
                "payload": {"S": json.dumps(value, separators=(",", ":"), allow_nan=False)}}
            for bucket, values in state.items() for key, value in values.items()
        }
        self.items["!revision"] = {"pk": {"S": "STATE"}, "sk": {"S": "!revision"}, "version": {"N": "1"}}
        self.commits = []
        self.fail_next = False
        self.conflicts = 0

    def get_item(self, **request):
        item = self.items.get(request["Key"]["sk"]["S"])
        return {} if item is None else {"Item": deepcopy(item)}

    def query(self, **request):
        assert request["ConsistentRead"] is True
        prefix = request["ExpressionAttributeValues"][":prefix"]["S"]
        after = request.get("ExclusiveStartKey", {}).get("sk", {}).get("S")
        reverse = not request.get("ScanIndexForward", True)
        names = sorted((key for key in self.items if key.startswith(prefix)
            and (after is None or (key < after if reverse else key > after))), reverse=reverse)
        selected = names[:request.get("Limit", len(names))]
        result = {"Items": [deepcopy(self.items[key]) for key in selected]}
        if len(selected) < len(names):
            result["LastEvaluatedKey"] = {"pk": {"S": "STATE"}, "sk": {"S": selected[-1]}}
        return result

    def transact_write_items(self, *, TransactItems):
        assert len(TransactItems) <= 100
        revision = TransactItems[0]["Put"]
        expected = int(revision.get("ExpressionAttributeValues", {}).get(":v", {"N": "0"})["N"])
        actual = int(self.items["!revision"]["version"]["N"])
        if self.fail_next:
            self.fail_next = False
            self.items["!revision"]["version"]["N"] = str(actual + 1)
            self.conflicts += 1
            raise self.exceptions.TransactionCanceledException()
        if actual != expected:
            self.conflicts += 1
            raise self.exceptions.TransactionCanceledException()
        proposed = deepcopy(self.items)
        written = []
        for operation in TransactItems:
            item = deepcopy(operation["Put"]["Item"])
            assert len(json.dumps(item).encode()) < 400 * 1024
            if "payload" in item:
                assert len(item["payload"]["S"].encode()) <= 300000
            key = item["sk"]["S"]
            proposed[key] = item
            written.append(key)
        self.items = proposed
        self.commits.append(written)

    def snapshot(self):
        result = empty()
        for key, item in self.items.items():
            if key == "!revision":
                continue
            bucket, ident = key.split(":", 1)
            result[bucket][ident] = json.loads(item["payload"]["S"])
        return result


class ObservedDynamoStore(DynamoStore):
    @property
    def state(self):
        # Existing graph helpers inspect a fresh durable read, never a route object.
        return self.client.snapshot()


@pytest.fixture
def dynamo_lab(accuracy_lab):
    original = accuracy_lab
    table = FakeDynamo(original[1].state)
    store = ObservedDynamoStore(table, "synthetic-table")
    app = AccuracyApplication(store, original[2], clock=lambda: original[3][0],
        accuracy_policies={"p": [original[5]]})
    return (app, store, original[2], original[3], original[4], original[5])


def detail(suite, cid):
    status, response = call(suite, "GET", f"/v1/projects/p/benchmark-accuracy/campaigns/{cid}")
    assert status == 200
    return response["campaign"]


def test_dynamo_ready_cancellation_is_durable_across_new_requests(dynamo_lab):
    suite = dynamo_lab
    campaign = create_accuracy(suite)
    cid = campaign["id"]
    status, response = call(suite, "POST", f"/v1/projects/p/benchmark-accuracy/campaigns/{cid}/cancel", {})
    assert status == 200 and response["campaign"]["status"] == "cancelled"
    assert detail(suite, cid)["status"] == "cancelled"
    state = suite[1].state
    task = next(iter(state["accuracy_tasks"].values()))
    row = next(iter(state["accuracy_allocations"].values()))["row"]
    assert task["status"] == row["status"] == "cancelled"
    assert row["not_submitted"] is True and row["reserved_gpu_seconds"] == 0
    assert row["allocated_gpu_seconds"] == row["allocated_cpu_seconds"] == 0
    assert state["accuracy_campaigns"]["p:alice:" + cid]["accounting"]["hnpe"]["unknown_allocations"] == 0


def test_dynamo_claim_lease_and_exact_ack_replay_persist(dynamo_lab):
    suite = dynamo_lab
    cid = create_accuracy(suite)["id"]
    claimed, frozen = claim_accuracy(suite)
    tid = claimed["task"]["id"]
    durable = suite[1].state["accuracy_tasks"][cid + ":" + tid]
    assert durable["lease"] == claimed["task"]["lease"]
    assert durable["status"] == "claimed"
    commits = len(suite[1].client.commits)
    status, replay = call(suite, "POST", "/v1/benchmark-connector/accuracy/claim", frozen)
    assert status == 200 and replay == claimed
    assert len(suite[1].client.commits) == commits
    changed = deepcopy(frozen)
    changed["campaign_id"] = cid
    changed["signature"] = base64.b64encode(suite[4].sign(p.claim_signing_bytes("alice", changed))).decode()
    assert call(suite, "POST", "/v1/benchmark-connector/accuracy/claim", changed)[0] == 409


def test_dynamo_event_closure_and_result_progression_are_atomic(dynamo_lab):
    suite = dynamo_lab
    cid = create_accuracy(suite)["id"]
    claimed, _ = claim_accuracy(suite)
    suite[1].client.fail_next = True
    graph.begin(suite, claimed, elapsed=7)
    assert suite[1].client.conflicts == 1
    tid = claimed["task"]["id"]
    state = suite[1].state
    task = state["accuracy_tasks"][cid + ":" + tid]
    row = state["accuracy_allocations"][cid + ":" + task["allocation_id"]]["row"]
    assert task["status"] == "publishing" and task["accounting_closed"] is True
    assert row["allocated_gpu_seconds"] == 7 and row["allocated_cpu_seconds"] == 112
    result = graph.native_result(suite, claimed, outputs={
        "guide_preparation": graph.receipt(suite, claimed, "guide"),
        "confirmation_commitment": "5" * 64},
        pins={"case_plan_sha256": "1"*64, "guide_case_set_sha256": "2"*64,
              "normalization_sha256": "3"*64, "validation_sha256": "4"*64})
    suite[1].client.fail_next = True
    status, response, frozen = send_event(suite, claimed, "result", "result", {"result": result})
    assert status == 200 and suite[1].client.conflicts == 2
    state = suite[1].state
    assert len(state["accuracy_tasks"]) == len(state["accuracy_allocations"]) == 2
    task = state["accuracy_tasks"][cid + ":" + tid]
    assert task["status"] == "succeeded" and task["lease"] is None
    assert task["result"]["record_key"] in state["requests"]
    campaign = state["accuracy_campaigns"]["p:alice:" + cid]
    assert campaign["phase"] == "guide_reference"
    assert campaign["accounting"]["hnpe"]["settled_gpu_seconds"] == 7
    commits = len(suite[1].client.commits)
    path = f"/v1/benchmark-connector/accuracy/campaigns/{cid}/tasks/{tid}/events"
    assert call(suite, "POST", path, frozen) == (200, response)
    assert len(suite[1].client.commits) == commits
    different = deepcopy(frozen)
    different["payload"]["result"]["scientific_status"] = "different"
    different["signature"] = base64.b64encode(suite[4].sign(p.event_signing_bytes("alice", different))).decode()
    assert call(suite, "POST", path, different)[0] == 409


def test_dynamo_expired_fence_recovery_and_active_cancellation(dynamo_lab):
    suite = dynamo_lab
    cid = create_accuracy(suite)["id"]
    claimed, _ = claim_accuracy(suite)
    status, _, _ = send_event(suite, claimed, "intent", "submitting", {
        "script_sha256": "f"*64, "intent_sha256": "1"*64, "marker": claimed["task"]["marker"]})
    assert status == 200
    status, _, _ = send_event(suite, claimed, "submitted", "submitted", {"slurm_job_id": "101"})
    assert status == 200
    old = event_body(suite, claimed, "old-fence", "heartbeat", {})
    suite[3][0] = claimed["task"]["lease"]["expires_at"] + 1
    recovered, _ = claim_accuracy(suite, "recover", cid)
    assert recovered["task"]["reconcile_only"] is True and recovered["task"]["lease_epoch"] == 2
    path = f"/v1/benchmark-connector/accuracy/campaigns/{cid}/tasks/{claimed['task']['id']}/events"
    assert call(suite, "POST", path, old)[0] == 409
    suite[1].client.fail_next = True
    assert call(suite, "POST", f"/v1/projects/p/benchmark-accuracy/campaigns/{cid}/cancel", {})[0] == 200
    assert detail(suite, cid)["status"] == "cancelling"
    state = suite[1].state
    task = state["accuracy_tasks"][cid + ":" + claimed["task"]["id"]]
    assert task["status"] == "cancelling" and task["slurm_job_id"] == "101"
    row = state["accuracy_allocations"][cid + ":" + task["allocation_id"]]["row"]
    assert row["status"] == "cancelling" and row["allocated_gpu_seconds"] is None
    refreshed, _ = claim_accuracy(suite, "cancel-refresh", cid)
    status, _, _ = send_event(suite, refreshed, "close", "allocation_closed", {
        "scheduler_accounting": {"source": "sacct", "site": "karolina", "slurm_job_id": "101",
            "state": "CANCELLED", "elapsed_seconds": 9, "allocated_gpus": 1, "cpu_seconds": 144}})
    assert status == 200
    assert send_event(suite, refreshed, "cancelled", "cancelled", {"not_submitted": False})[0] == 200
    assert detail(suite, cid)["status"] == "cancelled"
    row = next(iter(suite[1].state["accuracy_allocations"].values()))["row"]
    assert row["allocated_gpu_seconds"] == 9 and row["allocated_cpu_seconds"] == 144


def test_dynamo_full_graph_publication_and_archive_ack_persist(dynamo_lab, monkeypatch):
    graph.test_full_source_unmet_metadata_graph_has_all256_and_closed_final_costs(
        dynamo_lab, monkeypatch, False, False, None)
    state = dynamo_lab[1].state
    campaign = next(iter(state["accuracy_campaigns"].values()))
    assert campaign["status"] == "completed" and campaign["archive"]["status"] == "verified"
    assert all(task["status"] == "succeeded" for task in state["accuracy_tasks"].values())
    assert all(row["row"]["reserved_gpu_seconds"] == 0 for row in state["accuracy_allocations"].values())
    assert "accuracy_archive_receipt:" + campaign["id"] in state["requests"]


def test_dynamo_readonly_gets_and_missing_records_do_not_commit(dynamo_lab):
    suite = dynamo_lab
    cid = create_accuracy(suite)["id"]
    commits = len(suite[1].client.commits)
    assert detail(suite, cid)["status"] == "pending"
    assert call(suite, "GET", f"/v1/projects/p/benchmark-accuracy/campaigns/{cid}/accounting")[0] == 200
    assert call(suite, "GET", "/v1/projects/p/benchmark-accuracy/campaigns/missing")[0] == 404
    assert len(suite[1].client.commits) == commits
    assert "p:alice:missing" not in suite[1].state["accuracy_campaigns"]


def padded(value, limit, encoder):
    value = deepcopy(value)
    value["padding"] = ""
    value["padding"] = "x" * (limit - len(encoder(value)))
    assert len(encoder(value)) == limit
    return value


def gateway_event(path, method, raw, *, encoded=False):
    return {"version": "2.0", "routeKey": "ANY /v1/{proxy+}", "rawPath": path,
        "rawQueryString": "", "headers": {"host": "api.invalid", "content-type": "application/json"},
        "requestContext": {"http": {"method": method, "path": path, "sourceIp": "127.0.0.1"},
            "authorizer": {"jwt": {"claims": {"sub": "alice", "iss": "https://identity.invalid/pool",
                "client_id": "browser-client", "token_use": "access"}}}},
        "body": base64.b64encode(raw.encode()).decode() if encoded else raw, "isBase64Encoded": encoded}


@pytest.fixture
def configured_gateway(monkeypatch):
    monkeypatch.setenv("COGNITO_ISSUER", "https://identity.invalid/pool")
    monkeypatch.setenv("COGNITO_CLIENT_ID", "browser-client")
    monkeypatch.delenv("COGNITO_REMEMBERED_CLIENT_ID", raising=False)


def test_accuracy_canonical_event_boundary_and_default_cap_are_exact(accuracy_lab, monkeypatch):
    app = accuracy_lab[0]
    path = "/v1/benchmark-connector/accuracy/campaigns/c/tasks/t/events"
    body = padded({}, 262144, p.canonical)
    app.check_request_size("POST", path, body)
    body["padding"] += "x"
    with pytest.raises(APIError) as error:
        app.check_request_size("POST", path, body)
    assert error.value.status == 413
    # Also prove the inherited handle checks before loading or mutating a store.
    def forbidden_load():
        raise AssertionError("Oversize must fail before a store read")
    monkeypatch.setattr(app.store, "load", forbidden_load)
    with pytest.raises(APIError) as error:
        app.handle("POST", path, {"sub": "alice"}, body)
    assert error.value.status == 413
    ordinary = padded({}, 200000, lambda value: json.dumps(value, allow_nan=False).encode())
    for method, route in (("GET", path), ("POST", path + "/"), ("POST", "/v1/me"),
                          ("POST", "/v1/benchmark-connector/campaigns/c/stages/t/events")):
        app.check_request_size(method, route, ordinary)
        changed = deepcopy(ordinary); changed["padding"] += "x"
        with pytest.raises(APIError) as error:
            app.check_request_size(method, route, changed)
        assert error.value.status == 413


@pytest.mark.parametrize("encoded", [False, True])
def test_gateway_accuracy_wire_boundary_before_parse(configured_gateway, monkeypatch, encoded):
    path = "/v1/benchmark-connector/accuracy/campaigns/c/tasks/t/events"
    raw = "{}" + " " * (524288 - 2)
    called = []
    monkeypatch.setattr(handler, "_benchmark_adapter", lambda event, context:
        (called.append(True) or {"statusCode": 204, "body": ""}))
    assert handler.handler(gateway_event(path, "POST", raw, encoded=encoded), None)["statusCode"] == 204
    assert called == [True]
    assert handler.handler(gateway_event(path, "POST", raw + " ", encoded=encoded), None)["statusCode"] == 413
    assert called == [True]
    for method, route in (("GET", path), ("POST", path + "/")):
        assert handler.handler(gateway_event(route, method, "{}" + " "*199998, encoded=encoded), None)["statusCode"] == 204
        assert handler.handler(gateway_event(route, method, "{}" + " "*199999, encoded=encoded), None)["statusCode"] == 413


def test_gateway_overlong_base64_is_bounded_before_decode(configured_gateway, monkeypatch):
    path = "/v1/benchmark-connector/accuracy/campaigns/c/tasks/t/events"
    event = gateway_event(path, "POST", "{}")
    event.update(isBase64Encoded=True, body="A" * (4*((524288+2)//3) + 1))
    def forbidden(*args, **kwargs):
        raise AssertionError("Oversized base64 must not be decoded")
    monkeypatch.setattr(handler.base64, "b64decode", forbidden)
    assert handler.handler(event, None)["statusCode"] == 413


def test_maximal_source_result_and_real_sdk_wire_reach_deployed_api(
        accuracy_lab, configured_gateway, monkeypatch):
    suite = accuracy_lab
    cid = graph.prepared(suite, "maximal-wire")
    claimed = graph.claim_kind(suite, cid, "guide_reference")
    graph.begin(suite, claimed)
    result = graph.native_result(suite, claimed, qualification={"synthetic_source_metadata": ""})
    result["qualification"]["synthetic_source_metadata"] = "x" * (250000 - len(p.canonical(result)))
    root = suite[5]["native_root"] + "/" + cid
    assert len(p.canonical(result)) == 250000
    p.validate_result(result, claimed["task"]["manifest"], native_root=root)
    overflow = deepcopy(result); overflow["qualification"]["synthetic_source_metadata"] += "x"
    with pytest.raises(ValueError, match="exceeds bound"):
        p.validate_result(overflow, claimed["task"]["manifest"], native_root=root)
    frozen = event_body(suite, claimed, "maximal-source", "result", {"result": result})
    assert len(json.dumps(frozen, allow_nan=False).encode()) > 200000
    # Maximal protocol wrapper identifiers/token/signature still fit both caps;
    # a task event carries a manifest DIGEST, never a second embedded manifest.
    maximum = deepcopy(frozen)
    for name in ("project_id", "connector_id", "campaign_id", "task_id", "event_id"):
        maximum[name] = "a"*128
    maximum.update(lease_token="t"*256, attempt=2**63-1, lease_epoch=2**63-1,
        expected_task_revision=2**63-1, signature=base64.b64encode(b"s"*64).decode())
    p.validate_event(maximum)
    assert len(p.canonical(maximum)) < 262144
    assert len(json.dumps(maximum, allow_nan=False).encode()) < 524288
    monkeypatch.setattr(handler, "_application", suite[0])
    monkeypatch.setattr(handler, "_benchmark_adapter", None)
    seen = []
    class Opener:
        def open(self, request, timeout):
            seen.append(request.data)
            assert timeout == 30 and request.data == json.dumps(frozen, allow_nan=False).encode()
            response = handler.handler(gateway_event(urlsplit(request.full_url).path, "POST", request.data.decode()), None)
            assert response["statusCode"] == 200, response
            return io.BytesIO(response["body"].encode())
    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Opener())
    path = f"/v1/benchmark-connector/accuracy/campaigns/{cid}/tasks/{claimed['task']['id']}/events"
    response = API("https://api.invalid", "synthetic-token").request("POST", path, frozen)
    assert seen and response["task"]["status"] == "succeeded"
    assert suite[1].state["accuracy_tasks"][cid + ":" + claimed["task"]["id"]]["result"]
