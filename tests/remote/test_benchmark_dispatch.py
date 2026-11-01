"""Synthetic dispatch: no OAuth, AWS, real SSH or science."""
import copy
import threading
from types import SimpleNamespace
import pytest
from aws_mesouq.benchmark_dispatch import FairBenchmarkWorker, PROTOCOLS
from aws_mesouq.connector.api import APIError
from aws_mesouq.connector.auth import SessionExpired

class Journal:
    def __init__(self):
        self.data = {"owner_sub": "owner"}
        self.snapshots = []
    def save(self):
        self.snapshots.append(copy.deepcopy(self.data))

class Worker:
    def __init__(self, name, calls, action=None):
        self.name, self.calls, self.action = name, calls, action
    def sweep(self):
        self.calls.append(self.name)
        if self.action:
            self.action()
        return {"active": 0}

def pair(journal=None, *, first=None, second=None, stop=None):
    journal = journal or Journal()
    calls = []
    one, two = Worker(PROTOCOLS[0], calls, first), Worker(PROTOCOLS[1], calls, second)
    return FairBenchmarkWorker(one, two, journal, project="project", owner_sub="owner", stop=stop), journal, calls

def test_round_robin_keeps_both_protocols_progressing_without_zero_active_quiescence():
    worker, journal, calls = pair()
    assert worker.sweep()["attempted"] == 2
    assert worker.sweep()["attempted"] == 2
    assert calls == list(PROTOCOLS) * 2

def test_nonfatal_protocol_error_does_not_starve_other_protocol_or_retain_response():
    def fail():
        raise RuntimeError("private-response-body")
    worker, journal, calls = pair(first=fail)
    assert worker.sweep()["failures"] == {PROTOCOLS[0]: "RuntimeError"}
    assert calls == list(PROTOCOLS)
    assert "private-response" not in repr(journal.snapshots)

@pytest.mark.parametrize("error", [APIError(401,"auth","private"), APIError(403,"denied","private"), SessionExpired("private")])
def test_fatal_auth_failure_stops_new_work_and_retains_next_turn(error):
    def fail():
        raise error
    worker, journal, calls = pair(first=fail)
    with pytest.raises(type(error)):
        worker.sweep()
    assert calls == [PROTOCOLS[0]]
    assert journal.data["benchmark_dispatch"]["next_protocol"] == PROTOCOLS[1]
    assert "private" not in repr(journal.snapshots)

def test_interrupted_call_resumes_with_other_protocol_on_fresh_worker():
    def interrupt():
        raise KeyboardInterrupt()
    worker, journal, calls = pair(first=interrupt)
    with pytest.raises(KeyboardInterrupt):
        worker.sweep()
    resumed, _, resumed_calls = pair(journal)
    resumed.sweep()
    assert resumed_calls == [PROTOCOLS[1], PROTOCOLS[0]]

def test_stop_takes_effect_after_current_call_without_starting_or_cancelling_more():
    stop = threading.Event()
    worker, journal, calls = pair(first=stop.set, stop=stop)
    assert worker.sweep()["attempted"] == 1
    assert calls == [PROTOCOLS[0]]
    assert journal.data["benchmark_dispatch"]["next_protocol"] == PROTOCOLS[1]
    assert worker.sweep()["attempted"] == 0

@pytest.mark.parametrize("change", [
 lambda value:value.update(owner_sub="other"), lambda value:value.update(project_id="other"),
 lambda value:value.update(next_protocol="arbitrary-module"), lambda value:value.update(last_errors={})])
def test_namespace_owner_project_and_registered_protocols_are_fenced(change):
    worker, journal, calls = pair()
    change(journal.data["benchmark_dispatch"])
    with pytest.raises(ValueError):
        pair(journal)
    assert calls == []

def test_runtime_arguments_bind_only_registered_mixed_entrypoint_and_registry(tmp_path):
    from aws_mesouq import benchmark_cli as cli
    args=SimpleNamespace(api_url="https://api.example.test",project="project",operator_config=tmp_path/"operator.json",
      journal=tmp_path/"journal",poll_seconds=20,ssh_host="karolina",ssh_state_dir=tmp_path/"ssh-state",
      once=True,worker_kind="benchmark-all",accuracy_contexts=tmp_path/"contexts.json")
    arguments=cli._runtime_arguments(args)
    assert arguments[arguments.index("--worker-kind")+1]=="benchmark-all"
    assert arguments[arguments.index("--accuracy-contexts")+1]==str(args.accuracy_contexts)
    assert cli.WORKER_ENTRYPOINTS["benchmark-all"]=="aws_mesouq/benchmark_dispatch.py"
    assert "--accuracy-contexts" not in cli._runtime_arguments(args,kind="benchmark-v1")
    args.accuracy_contexts=None
    with pytest.raises(cli.BrokerError,match="accuracy_contexts_required"):
        cli._runtime_arguments(args)
    with pytest.raises(cli.BrokerError,match="unknown_worker_kind"):
        cli._runtime_arguments(args,kind="arbitrary-module")

def test_managed_mixed_worker_shares_one_journal_transport_api_without_login(tmp_path,monkeypatch):
    from aws_mesouq import benchmark_cli as cli,benchmark_worker as legacy,benchmark_dispatch as dispatch
    from aws_mesouq.connector import journal as jm,slurm as sm
    counts={"journal":0,"transport":0,"api":0,"journal_close":0,"transport_close":0};calls=[]
    class SharedJournal(Journal):
        def __init__(self,path):
            super().__init__();counts["journal"]+=1
        def close(self):counts["journal_close"]+=1
    class Transport:
        def __init__(self,**kwargs):counts["transport"]+=1
        def identity(self):return "approved-user"
        def run(self,args):
            assert args==["hostname","-f"];return "approved-host"
        def close(self):counts["transport_close"]+=1
    class Slurm:
        def __init__(self,transport):self.transport=transport
    class Legacy:
        def __init__(self,api,slurm,journal,**kwargs):
            assert kwargs["propagate_auth_errors"] is True
            counts["shared"]=(api,slurm,journal)
        def sweep(self):calls.append(PROTOCOLS[0])
    def accuracy(api,slurm,journal,**kwargs):
        assert (api,slurm,journal)==counts["shared"]
        assert kwargs=={"project":"project","registry_path":tmp_path/"contexts.json"}
        return Worker(PROTOCOLS[1],calls)
    def api(*args):counts["api"]+=1;return object()
    monkeypatch.setattr(cli.BrokerClient,"from_environment",lambda:object())
    monkeypatch.setattr(legacy,"validate_operator",lambda value:{"expected_user":"approved-user","expected_host":"approved-host"})
    monkeypatch.setattr(legacy,"existing_identity",lambda journal:object())
    monkeypatch.setattr(legacy,"BenchmarkWorker",Legacy)
    monkeypatch.setattr(jm,"Journal",SharedJournal);monkeypatch.setattr(sm,"Slurm",Slurm)
    monkeypatch.setattr(dispatch,"build_accuracy_worker",accuracy)
    monkeypatch.setattr(cli,"browser_login",lambda *args:pytest.fail("managed worker must not log in"))
    operator=tmp_path/"operator.json";operator.write_text("{}")
    args=SimpleNamespace(api_url="https://api.example.test",project="project",operator_config=operator,
       journal=tmp_path/"journal",ssh_host="karolina",ssh_state_dir=tmp_path/"ssh-state",local_cluster=False,
       once=True,poll_seconds=20,worker_kind="benchmark-all",accuracy_contexts=tmp_path/"contexts.json")
    assert cli.run_managed_worker(args,api_factory=api,transport_factory=Transport)==0
    assert calls==list(PROTOCOLS)
    assert {k:counts[k] for k in ("journal","transport","api","journal_close","transport_close")}=={
       "journal":1,"transport":1,"api":1,"journal_close":1,"transport_close":1}

def test_accuracy_factory_uses_real_sdk_with_the_shared_instances(tmp_path,monkeypatch):
    from aws_mesouq.benchmark_dispatch import build_accuracy_worker
    from aws_mesouq.benchmark_accuracy_contexts import AccuracyExecutionContexts
    from aws_mesouq.benchmark_accuracy_worker import AccuracyWorker
    from aws_mesouq.benchmark_accuracy_client import AccuracyClient
    from aws_mesouq.adapters.benchmark_accuracy import AccuracyNativeExecutor
    context=SimpleNamespace(project="project",owner_sub="owner")
    seen=[]
    def registry(cls,path,**kwargs):
        seen.append((path,kwargs));return context
    monkeypatch.setattr(AccuracyExecutionContexts,"from_file",classmethod(registry))
    api=object();slurm=SimpleNamespace(transport=object());journal=Journal();path=tmp_path/"contexts.json"
    worker=build_accuracy_worker(api,slurm,journal,project="project",registry_path=path)
    assert isinstance(worker,AccuracyWorker) and isinstance(worker.client,AccuracyClient)
    assert isinstance(worker.executor,AccuracyNativeExecutor)
    assert worker.client.api is api and worker.client.journal is journal
    assert worker.slurm is slurm and worker.journal is journal and worker.contexts is context
    assert seen==[(path,{"project":"project","owner_sub":"owner"})]
    assert journal.data["benchmark_accuracy"]["owner_sub"]=="owner"
