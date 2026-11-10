"""Future-only server invocation fencing; synthetic HTTP/controller/Slurm only."""
from copy import deepcopy
import hashlib, types, uuid
import pytest

from test_benchmark import lab
from test_accuracy import accuracy_lab, spec
from test_accuracy_sdk import registry, sdk
from test_accuracy_worker import Journal, Slurm, Executor, make_worker, intent, restart
from test_accuracy_dynamo_persistence import dynamo_lab
from aws_mesouq import benchmark_accuracy_protocol as p
from aws_mesouq.benchmark_accuracy_contexts import AccuracyExecutionContexts
from aws_mesouq.benchmark_accuracy_worker import AccuracyWorker, AccuracyWorkerBlocked
from aws_mesouq.connector.api import APIError as ConnectorAPIError


@pytest.fixture(params=["accuracy_lab","dynamo_lab"])
def fenced_lab(request):
    return request.getfixturevalue(request.param)


def future_worker(suite, monkeypatch, *, native_root=None, archive_root=None):
    document, context = registry(suite)
    context["capabilities"] = sorted(context["capabilities"]+[p.INVOCATION_PROTOCOL])
    if native_root is not None:
        context["native_root"]=str(native_root)
        context["archive_root"]=str(archive_root)
    context["id"] = p.context_id(context)
    document["contexts"][0]["context"] = context
    suite[0].accuracy_policies["p"] = [context]
    contexts = AccuracyExecutionContexts(document,project="p",owner_sub="alice")
    client,api = sdk(suite,monkeypatch)
    journal,slurm=Journal(),Slurm()
    executor=Executor(contexts,slurm);executor.clock=lambda:suite[3][0]
    worker=AccuracyWorker(client,slurm,journal,project="p",contexts=contexts,
        executor=executor,clock=lambda:suite[3][0])
    campaign=client.create("p",spec())["campaign"]
    entry=worker._claim_one()
    assert entry["claim"]["campaign"]["id"]==campaign["id"]
    assert entry["claim"]["task"]["manifest"]["submission_protocol"]==p.INVOCATION_PROTOCOL
    return worker,entry,client,api,journal,slurm,executor,contexts


def ledger(parts):
    return parts[2].accounting("p",parts[1]["claim"]["campaign"]["id"])["snapshot"]["allocations"][0]


def grant_payload(entry,nonce="a"*32):
    prep=entry["native_preparation"]
    intent={k:prep[k] for k in ("script_sha256","intent_sha256","marker")}
    return {"grant_request_nonce":nonce,"submission_intent_sha256":p.checksum(intent)}


def authorize(parts,nonce="a"*32,event_id=None):
    worker,entry,client=parts[:3]
    body=client.build_event_body("p",entry["claim"],event_id or "grant-"+uuid.uuid4().hex,
        "authorize_invocation",grant_payload(entry,nonce))
    return body,client.event_frozen(body)


def test_cancel_before_grant_is_server_fenced_zero_with_distinct_receipt(fenced_lab,monkeypatch):
    parts=future_worker(fenced_lab,monkeypatch);intent(parts)
    worker,entry,client,api,journal,slurm,executor,contexts=parts
    client.cancel("p",entry["claim"]["campaign"]["id"])
    row=ledger(parts)
    assert row["not_submitted"] is True and row["reserved_gpu_seconds"]==0
    assert row["allocated_gpu_seconds"]==row["allocated_cpu_seconds"]==0
    detail=client.task("p",entry["claim"]["campaign"]["id"],entry["claim"]["task"]["id"])
    assert detail["task"]["recovery"]["code"]=="server_invocation_never_authorized"
    with pytest.raises(ConnectorAPIError):
        authorize(parts)
    worker.tick(entry)
    assert entry["phase"]=="terminal" and slurm.submit_calls==[]
    first=deepcopy(ledger(parts));client.cancel("p",entry["claim"]["campaign"]["id"])
    assert ledger(parts)==first


def test_grant_before_cancel_retains_unknown_costs_and_same_grant_replay_is_not_fresh(fenced_lab,monkeypatch):
    parts=future_worker(fenced_lab,monkeypatch);intent(parts)
    body,response=authorize(parts)
    assert response["invocation_authorization_fresh"] is True
    replay=parts[2].event_frozen(body)
    assert replay["invocation_authorization_fresh"] is False
    assert replay["invocation_authorization"]==response["invocation_authorization"]
    parts[2].cancel("p",parts[1]["claim"]["campaign"]["id"])
    row=ledger(parts)
    assert row["not_submitted"] is False and row["allocated_gpu_seconds"] is None
    assert row["reserved_gpu_seconds"]>0 and parts[5].submit_calls==[]
    detail=parts[2].task("p",parts[1]["claim"]["campaign"]["id"],parts[1]["claim"]["task"]["id"])
    assert detail["task"]["recovery"]["code"]=="invocation_authorized_unreconciled"


def test_first_grant_ack_in_current_call_invokes_once(fenced_lab,monkeypatch):
    parts=future_worker(fenced_lab,monkeypatch);intent(parts)
    parts[0].tick(parts[1])
    assert len(parts[5].submit_calls)==1 and parts[1]["phase"]=="job_known"
    parts[0].tick(parts[1]);assert len(parts[5].submit_calls)==1
    assert ledger(parts)["not_submitted"] is False


def test_grant_ack_lost_then_replayed_is_reconcile_only_even_same_process(fenced_lab,monkeypatch):
    parts=future_worker(fenced_lab,monkeypatch);intent(parts)
    worker,entry,client,api,journal,slurm,executor,contexts=parts
    request=api.request;lost=[]
    def lose(method,path,payload=None):
        response=request(method,path,payload)
        if payload and payload.get("type")=="authorize_invocation" and not lost:
            lost.append(deepcopy(payload));raise ConnectionError("synthetic lost grant ACK")
        return response
    monkeypatch.setattr(api,"request",lose)
    with pytest.raises(ConnectionError):worker.tick(entry)
    assert slurm.submit_calls==[] and entry["pending_event"] is not None
    worker.tick(entry);assert entry["phase"]=="invocation_authorized"
    worker.tick(entry);assert entry["phase"]=="reconciling"
    assert slurm.submit_calls==[] and ledger(parts)["reserved_gpu_seconds"]>0


def test_grant_pending_after_restart_never_authorizes_native_call(fenced_lab,monkeypatch):
    parts=future_worker(fenced_lab,monkeypatch);intent(parts)
    worker,entry,client,api,journal,slurm,executor,contexts=parts
    request=api.request
    def fail_before(method,path,payload=None):
        if payload and payload.get("type")=="authorize_invocation":
            raise ConnectionError("synthetic request did not reach backend")
        return request(method,path,payload)
    monkeypatch.setattr(api,"request",fail_before)
    with pytest.raises(ConnectionError):worker.tick(entry)
    monkeypatch.setattr(api,"request",request)
    parts=restart(parts);worker,entry=parts[:2]
    worker.tick(entry) # The first server application still cannot revive a call stack.
    assert entry["phase"]=="invocation_authorized" and parts[5].submit_calls==[]
    worker.tick(entry)
    assert entry["phase"]=="reconciling" and parts[5].submit_calls==[]


def test_pregrant_journal_rollback_after_real_invoke_never_submits_second_job(fenced_lab,monkeypatch):
    parts=future_worker(fenced_lab,monkeypatch);intent(parts)
    snapshot=deepcopy(parts[4].persisted)
    parts[0].tick(parts[1]);assert len(parts[5].submit_calls)==1
    worker,entry,client,api,journal,slurm,executor,contexts=parts
    # Hide scheduler visibility to make absence adversarial rather than useful.
    slurm.jobs={}
    restored=Journal(snapshot)
    resumed=AccuracyWorker(client,slurm,restored,project="p",contexts=contexts,
        executor=executor,clock=worker.clock)
    old=next(iter(resumed.state["tasks"].values()))
    resumed.tick(old)
    assert old["phase"]=="reconciling" and len(slurm.submit_calls)==1
    resumed.tick(old);assert len(slurm.submit_calls)==1
    assert ledger(parts)["reserved_gpu_seconds"]>0


@pytest.mark.parametrize("mutation",["nonce","intent","manifest","epoch","revision"])
def test_grant_rejects_forged_or_stale_bindings(fenced_lab,monkeypatch,mutation):
    parts=future_worker(fenced_lab,monkeypatch);intent(parts)
    worker,entry,client=parts[:3]
    body=client.build_event_body("p",entry["claim"],"grant-forged","authorize_invocation",grant_payload(entry))
    if mutation=="nonce":body["payload"]["grant_request_nonce"]="bad"
    elif mutation=="intent":body["payload"]["submission_intent_sha256"]="f"*64
    elif mutation=="manifest":body["manifest_sha256"]="f"*64
    elif mutation=="epoch":body["lease_epoch"]+=1
    else:body["expected_task_revision"]-=1
    with pytest.raises((ConnectorAPIError,ValueError)):client.event_frozen(body)
    assert parts[5].submit_calls==[]
    client.cancel("p",entry["claim"]["campaign"]["id"])
    assert ledger(parts)["not_submitted"] is True # No grant was ever committed.


@pytest.mark.parametrize("field,value",[("invocation_authorization_fresh",False),
    ("invocation_authorization_fresh",1),("invocation_authorization",{}),("authorization_nonce","f"*32)])
def test_malformed_or_nonfresh_ack_grants_no_native_authority(fenced_lab,monkeypatch,field,value):
    parts=future_worker(fenced_lab,monkeypatch);intent(parts)
    request=parts[3].request
    def alter(method,path,payload=None):
        result=request(method,path,payload)
        if payload and payload.get("type")=="authorize_invocation":
            if field=="authorization_nonce":result["invocation_authorization"]["grant_request_nonce"]=value
            else:result[field]=value
        return result
    monkeypatch.setattr(parts[3],"request",alter)
    try:parts[0].tick(parts[1])
    except (ValueError,AccuracyWorkerBlocked):pass
    assert parts[5].submit_calls==[]
    parts[2].cancel("p",parts[1]["claim"]["campaign"]["id"])
    assert ledger(parts)["not_submitted"] is False # Actual server grant still matters.


def test_cancel_races_after_fresh_grant_before_native_submit(fenced_lab,monkeypatch):
    parts=future_worker(fenced_lab,monkeypatch);intent(parts)
    request=parts[3].request
    def cancel_after(method,path,payload=None):
        result=request(method,path,payload)
        if payload and payload.get("type")=="authorize_invocation":
            parts[2].cancel("p",parts[1]["claim"]["campaign"]["id"])
        return result
    monkeypatch.setattr(parts[3],"request",cancel_after)
    parts[0].tick(parts[1])
    assert parts[5].submit_calls==[] and parts[1]["phase"]=="reconciling"
    assert ledger(parts)["not_submitted"] is False and ledger(parts)["reserved_gpu_seconds"]>0


def test_legacy_has_no_version_marker_and_never_gains_zero_from_missing_producer(accuracy_lab,monkeypatch):
    parts=make_worker(accuracy_lab,monkeypatch);intent(parts)
    assert "submission_protocol" not in parts[1]["claim"]["task"]["manifest"]
    parts[2].cancel("p",parts[1]["claim"]["campaign"]["id"])
    parts[0].tick(parts[1])
    assert parts[1]["phase"]=="reconciling" and parts[5].submit_calls==[]
    row=ledger(parts)
    assert row["not_submitted"] is False and row["reserved_gpu_seconds"]>0
    detail=parts[2].task("p",parts[1]["claim"]["campaign"]["id"],parts[1]["claim"]["task"]["id"])
    assert detail["task"]["recovery"]["code"]=="submission_identity_unknown"


def test_legacy_zero_guard_unchanged_after_intent(accuracy_lab,monkeypatch):
    parts=make_worker(accuracy_lab,monkeypatch);intent(parts)
    parts[2].cancel("p",parts[1]["claim"]["campaign"]["id"])
    parts[0]._refresh(parts[1],force=True)
    body=parts[2].build_event_body("p",parts[1]["claim"],"unsafe-zero","cancelled",{"not_submitted":True})
    with pytest.raises(ConnectorAPIError) as caught:parts[2].event_frozen(body)
    assert caught.value.code=="accuracy_submission_ambiguous"
    assert ledger(parts)["allocated_gpu_seconds"] is None


def test_dynamo_conditional_grant_failure_is_atomic_and_replay_safe(dynamo_lab,monkeypatch):
    from aws_publishing.storage import Conflict
    parts=future_worker(dynamo_lab,monkeypatch);intent(parts)
    table=dynamo_lab[1].client
    before=deepcopy(table.snapshot())
    table.fail_next=True
    try:
        body,response=authorize(parts)
    except (Conflict,ConnectorAPIError):
        assert table.snapshot()==before
        parts[2].cancel("p",parts[1]["claim"]["campaign"]["id"])
        assert ledger(parts)["not_submitted"] is True
    else:
        assert response["invocation_authorization_fresh"] is True
        assert parts[2].event_frozen(body)["invocation_authorization_fresh"] is False
        parts[2].cancel("p",parts[1]["claim"]["campaign"]["id"])
        assert ledger(parts)["not_submitted"] is False
    assert table.conflicts>0 and parts[5].submit_calls==[]


def test_future_server_abort_uses_normal_archive_eligibility_and_snapshot(accuracy_lab,monkeypatch,tmp_path):
    parts=future_worker(accuracy_lab,monkeypatch);intent(parts)
    worker,entry,client,api=parts[:4];cid=entry["claim"]["campaign"]["id"]
    client.cancel("p",cid);worker.tick(entry)
    snapshot=client.accounting("p",cid)["snapshot"]
    p.validate_snapshot(snapshot,allow_unprepared=True)
    campaign=next(c for c in client.campaigns("p",archived="false")["campaigns"] if c["id"]==cid)
    assert campaign["status"]=="cancelled"
    assert campaign["archive"]["available"] is False and "native" in campaign["archive"]["reason"].lower()
    with pytest.raises(ConnectorAPIError):
        api.request("POST","/v1/projects/p/benchmark-accuracy/campaigns/"+cid+"/archive",{})
    assert client.accounting("p",cid)["snapshot"]==snapshot
    assert parts[5].submit_calls==[]


def test_current_disabled_context_cannot_gain_new_invocation_but_can_cancel(fenced_lab,monkeypatch):
    parts=future_worker(fenced_lab,monkeypatch);intent(parts)
    fenced_lab[0].accuracy_policies["p"][0]["enabled"]=False
    with pytest.raises(ConnectorAPIError) as caught:authorize(parts)
    assert caught.value.code=="accuracy_invocation_forbidden"
    parts[2].cancel("p",parts[1]["claim"]["campaign"]["id"])
    assert ledger(parts)["not_submitted"] is True and parts[5].submit_calls==[]


def test_transferred_original_lease_cannot_authorize_invocation(fenced_lab,monkeypatch):
    parts=future_worker(fenced_lab,monkeypatch);intent(parts)
    old_epoch=parts[1]["claim"]["task"]["lease"]["epoch"]
    fenced_lab[3][0]+=fenced_lab[0].lease_seconds+1
    parts[0]._refresh(parts[1],force=True)
    assert parts[1]["claim"]["task"]["lease"]["epoch"]>old_epoch
    with pytest.raises(ConnectorAPIError) as caught:authorize(parts)
    assert caught.value.code in {"accuracy_invocation_fence_conflict","accuracy_invocation_forbidden"}
    parts[2].cancel("p",parts[1]["claim"]["campaign"]["id"])
    assert ledger(parts)["not_submitted"] is True and parts[5].submit_calls==[]


@pytest.mark.parametrize("ref",["0369bfa6cad62517a4e01dcbaee0207c2955a2a5","ede35e7d0cfa39bf9ec6dca0af27fbfd0c6bf44f"])
def test_actual_old_sdk_parses_legacy_private_claim_and_ack_wire(ref,accuracy_lab,monkeypatch):
    from pathlib import Path
    root=Path(__file__).parents[3]
    def old_module(relative,name):
        raw = _legacy_source(ref, relative)
        module=types.ModuleType(name);module.__package__="aws_mesouq";module.__file__=str(root/relative)
        exec(compile(raw,module.__file__,"exec",dont_inherit=True),module.__dict__)
        return module
    oldp=old_module("src/aws_mesouq/benchmark_accuracy_protocol.py","aws_mesouq._old_protocol_test")
    oldc=old_module("src/aws_mesouq/benchmark_accuracy_contexts.py","aws_mesouq._old_contexts_test")
    oldc.protocol=oldp
    parts=make_worker(accuracy_lab,monkeypatch)
    claim=deepcopy(parts[1]["claim"])
    assert "recovery" not in claim["campaign"] and "recovery" not in claim["task"]
    context=parts[7].context(parts[1]["context_id"])
    context.pop("source_qualification",None)
    context["id"]=oldp.context_id(context)
    claim["campaign"]["execution_context_id"]=context["id"]
    claim["task"]["manifest"]["context_id"]=context["id"]
    claim["task"]["manifest_sha256"]=oldp.checksum(claim["task"]["manifest"])
    operator=parts[7].operator(parts[1]["context_id"])
    registry={"schema":oldc.REGISTRY_SCHEMA,"contexts":[{"context":context,"operator":operator}]}
    oldcontexts=oldc.AccuracyExecutionContexts(registry,project="p",owner_sub="alice")
    assert oldcontexts.validate_claim(claim)==claim
    response=parts[0]._event(parts[1],"heartbeat",{})
    assert "recovery" not in response["task"] and "recovery" not in response["campaign"]


@pytest.mark.parametrize("evidence",["task_job","row_job","clock","scheduler_accounting",
    "gpu_cost","cpu_cost","closed","running","publishing","artifact","row_status","missing_cost"])
def test_inconsistent_native_evidence_can_never_be_zero_closed(evidence,accuracy_lab,monkeypatch):
    parts=future_worker(accuracy_lab,monkeypatch);intent(parts)
    cid=parts[1]["claim"]["campaign"]["id"];tid=parts[1]["claim"]["task"]["id"]
    state=accuracy_lab[1].state
    task=state["accuracy_tasks"][cid+":"+tid]
    row=state["accuracy_allocations"][cid+":"+task["allocation_id"]]["row"]
    if evidence=="task_job":task["slurm_job_id"]="123"
    elif evidence=="row_job":row["slurm_job_id"]="123"
    elif evidence=="clock":task["native_clock"]={"job_id":"123","start":1,"end":61}
    elif evidence=="scheduler_accounting":task["scheduler_accounting"]={"slurm_job_id":"123"}
    elif evidence=="gpu_cost":row["allocated_gpu_seconds"]=7
    elif evidence=="cpu_cost":row["allocated_cpu_seconds"]=9
    elif evidence=="closed":task["accounting_closed"]=True
    elif evidence=="running":task["status"]="running"
    elif evidence=="publishing":task["status"]="publishing"
    elif evidence=="artifact":task["artifact_ids"]=["synthetic-output"]
    elif evidence=="row_status":row["status"]="running"
    else:row.pop("allocated_cpu_seconds")
    before=deepcopy(state)
    with pytest.raises(ConnectorAPIError) as caught:parts[2].cancel("p",cid)
    assert caught.value.code=="accuracy_invocation_abort_forbidden"
    assert accuracy_lab[1].state==before
    assert row["not_submitted"] is False and parts[5].submit_calls==[]


def test_future_producer_event_without_server_grant_is_rejected(fenced_lab,monkeypatch):
    parts=future_worker(fenced_lab,monkeypatch);intent(parts)
    body=parts[2].build_event_body("p",parts[1]["claim"],"ungranted-producer",
        "submitted",{"slurm_job_id":"123"})
    with pytest.raises(ConnectorAPIError) as caught:parts[2].event_frozen(body)
    assert caught.value.code=="accuracy_invocation_not_authorized"
    assert ledger(parts)["allocated_gpu_seconds"] is None


def test_future_known_native_costs_still_follow_normal_cancelled_archive(accuracy_lab,monkeypatch,tmp_path):
    from aws_mesouq.connector.transport import Transport
    native,archive=tmp_path/"native",tmp_path/"project"
    native.mkdir(mode=0o700);archive.mkdir(mode=0o700)
    parts=future_worker(accuracy_lab,monkeypatch,native_root=native,archive_root=archive)
    intent(parts)
    worker,entry,client,api,journal,slurm,executor,contexts=parts
    worker.tick(entry);worker.tick(entry)
    assert entry["phase"]=="waiting" and len(slurm.submit_calls)==1
    cid=entry["claim"]["campaign"]["id"];job=entry["job_id"]
    client.cancel("p",cid)
    executor.observation={"job_id":job,"state":"terminal","mechanical_failure":True,
        "accounting":{"source":"sacct","site":"karolina","slurm_job_id":job,
            "state":"CANCELLED","elapsed_seconds":2,"allocated_gpus":1,"cpu_seconds":32}}
    worker.tick(entry);worker.tick(entry)
    assert entry["phase"]=="terminal"
    snapshot=client.accounting("p",cid)["snapshot"]
    p.validate_snapshot(snapshot,allow_unprepared=True)
    assert snapshot["allocations"][0]["slurm_job_id"]==job
    assert snapshot["allocations"][0]["allocated_gpu_seconds"]==2
    assert snapshot["allocations"][0]["not_submitted"] is False
    campaign=next(c for c in client.campaigns("p",archived="false")["campaigns"] if c["id"]==cid)
    assert campaign["status"]=="cancelled" and campaign["archive"]["available"] is True
    root=native/cid;root.mkdir(mode=0o700)
    payload=root/"synthetic-native-evidence.json"
    payload.write_bytes(p.canonical({"job_id":job,"kind":"synthetic-only-evidence"}));payload.chmod(0o600)
    slurm.transport=Transport(local=True)
    api.request("POST","/v1/projects/p/benchmark-accuracy/campaigns/"+cid+"/archive",{})
    for unused in range(12):
        worker._archive_one()
        record=worker.state["archives"].get(cid)
        if record and record["phase"]=="verified":break
    assert worker.state["archives"][cid]["phase"]=="verified"
    assert client.accounting("p",cid)["snapshot"]==snapshot
    assert len(slurm.submit_calls)==1


def test_cancelling_detail_with_missing_active_metadata_stays_pageable_but_direct_task_is_404(accuracy_lab,monkeypatch):
    parts=future_worker(accuracy_lab,monkeypatch);intent(parts)
    authorize(parts)
    cid=parts[1]["claim"]["campaign"]["id"];tid=parts[1]["claim"]["task"]["id"]
    state=accuracy_lab[1].state
    campaign=state["accuracy_campaigns"]["p:alice:"+cid]
    task=state["accuracy_tasks"][cid+":"+tid]
    missing="t99999999-"+"f"*32
    campaign["status"]="cancelling";campaign["active_tasks"].append(missing)
    task["status"]="cancelling"
    before=deepcopy(state["accuracy_allocations"])
    detail=parts[3].request("GET","/v1/projects/p/benchmark-accuracy/campaigns/"+cid)
    assert detail["campaign"]["status"]=="cancelling"
    assert detail["campaign"]["tasks"]
    assert detail["campaign"]["recovery"]["state"]=="blocked_reconciliation"
    with pytest.raises(ConnectorAPIError) as caught:parts[2].task("p",cid,missing)
    assert caught.value.status==404
    assert accuracy_lab[1].state["accuracy_allocations"]==before


def _legacy_source(ref, relative):
    import importlib.util
    from pathlib import Path
    root = Path(__file__).resolve().parents[3]
    path = root / "tests" / "fixtures" / "accuracy_legacy_protocol" / "loader.py"
    spec = importlib.util.spec_from_file_location("historical_accuracy_source_fixtures", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.load_source(root, ref, relative)
