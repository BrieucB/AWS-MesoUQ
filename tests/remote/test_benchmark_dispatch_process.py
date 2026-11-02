"""Registered real child upgrades using source-bound synthetic external boundaries."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from test_benchmark_cli import cli_rig, observations, wait_for
from aws_mesouq import benchmark_cli as cli
from aws_mesouq.connector.benchmark_supervisor import source_binding
from aws_mesouq.connector.journal import Journal

_BOUNDARIES = r"""
import hashlib,json,os,urllib.request,urllib.error
from pathlib import Path
from types import SimpleNamespace
from aws_mesouq.connector.api import API
from aws_mesouq.connector.transport import Transport
from aws_mesouq.connector.ssh_session import SSHSessionError
CONTROL=json.loads(Path(CONTROL_PATH_LITERAL).read_text())
ACTIVE_KIND=None

def observe(value):
    fd=os.open(CONTROL["observations"],os.O_WRONLY|os.O_APPEND|os.O_CREAT,0o600)
    try:os.write(fd,(json.dumps({"pid":os.getpid(),**value},sort_keys=True)+"\n").encode())
    finally:os.close(fd)

class SyntheticTransport(Transport):
    def identity(self):return "fixtureuser"
    def check_healthy(self):
        if Path(CONTROL["fail_transport"]).exists():raise SSHSessionError("connection_lost")
    def run(self,argv,*,data=None):
        if argv==["hostname","-f"]:return "fixture.cluster\n"
        raise AssertionError("No native operations allowed in registered dispatcher fixture")

class SyntheticAPI(API):
    def request(self,method,path,payload=None):
        token=self.token.token()
        if method=="GET" and "/benchmark-accuracy/campaigns" in path:
            result={"campaigns":[],"next_cursor":None}
        else:
            request=urllib.request.Request(CONTROL["endpoint"]+path,
                data=None if payload is None else json.dumps(payload).encode(),method=method,
                headers={"Authorization":"Bearer "+token,"Content-Type":"application/json"})
            with urllib.request.urlopen(request,timeout=5) as response:result=json.load(response)
        observe({"method":method,"path":path,"kind":ACTIVE_KIND})
        return result
    def transfer(self,*args,**kwargs):
        raise AssertionError("No artifact transfers allowed in registered dispatcher fixture")

def install(scope):
    import aws_mesouq.benchmark_dispatch as dispatch
    real=scope["run_managed_worker"]
    def accuracy(api,slurm,journal,*,project,registry_path):
        from aws_mesouq.benchmark_accuracy_contexts import read_registry
        from aws_mesouq.benchmark_accuracy_client import AccuracyClient
        from aws_mesouq.benchmark_accuracy_worker import AccuracyWorker
        document=read_registry(registry_path)
        if document!={"schema":"synthetic-unsubmitted-registry.v1","marker":"same-registry"}:
            raise AssertionError("Synthetic registry changed")
        observe({"phase":"accuracy_registry","registry_path":str(registry_path),
                 "registry_sha256":hashlib.sha256(Path(registry_path).read_bytes()).hexdigest()})
        # No context is admitted: actual SDK/worker paths remain observation-only.
        contexts=SimpleNamespace(project=project,owner_sub=journal.data["owner_sub"],supported=lambda:[])
        executor=SimpleNamespace(admitted_contexts=lambda:[])
        return AccuracyWorker(AccuracyClient(api,journal,journal.data["owner_sub"]),slurm,journal,
                              project=project,contexts=contexts,executor=executor)
    dispatch.build_accuracy_worker=accuracy
    def run(args,*,stop=None,**unused):
        global ACTIVE_KIND
        ACTIVE_KIND=args.worker_kind
        observe({"phase":"entry","kind":ACTIVE_KIND,
                 "loaded_cli_path":scope["__file__"],"loaded_cli_sha256":hashlib.sha256(Path(scope["__file__"]).read_bytes()).hexdigest(),
                 "loaded_dispatch_path":dispatch.__file__,"loaded_dispatch_sha256":hashlib.sha256(Path(dispatch.__file__).read_bytes()).hexdigest()})
        code=real(args,api_factory=SyntheticAPI,transport_factory=SyntheticTransport,stop=stop)
        observe({"phase":"exit","code":code,"kind":ACTIVE_KIND})
        return code
    scope["run_managed_worker"]=run
"""


def registered(rig, tmp_path, *, initial="benchmark-all"):
    package = tmp_path/"registered-package"
    shutil.copytree(rig.package,package)
    control = tmp_path/"registered-boundaries.json"
    control.write_bytes(rig.control_path.read_bytes())
    control.chmod(0o600)
    helper = package/"aws_mesouq/_dispatch_qa_boundaries.py"
    helper.write_text(_BOUNDARIES.replace("CONTROL_PATH_LITERAL",repr(str(control))))
    source = package/"aws_mesouq/benchmark_cli.py"
    text = source.read_text()
    marker = 'if __name__ == "__main__":\n'
    assert text.count(marker)==1
    text=text.replace(marker,'from aws_mesouq._dispatch_qa_boundaries import install as _install_qa_boundaries\n_install_qa_boundaries(globals())\n\n'+marker)
    source.write_text(text)
    for directory,dirs,files in os.walk(package):
        Path(directory).chmod(0o700)
        for name in files:(Path(directory)/name).chmod(0o600)
    hashes={p.relative_to(package).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in package.rglob("*") if p.is_file()}
    registry=tmp_path/"reviewed-registry.json"
    registry.write_text(json.dumps({"schema":"synthetic-unsubmitted-registry.v1","marker":"same-registry"},sort_keys=True))
    registry.chmod(0o600)
    manifests={}
    for kind in ("benchmark-v1","benchmark-all"):
        entry=cli.WORKER_ENTRYPOINTS[kind]
        binding=source_binding(entry,hashes)
        value={"schema":"aws_mesouq.approved_source_worker.v1","entrypoint":entry,
               "files":hashes,"binding_sha256":binding}
        path=tmp_path/(kind+"-manifest.json")
        path.write_text(json.dumps(value,sort_keys=True));path.chmod(0o600)
        manifests[kind]=(path,binding)
    parser=argparse.ArgumentParser();cli._runtime_parser(parser)
    values=[*rig.arguments[1:],"--worker-kind",initial]
    if initial=="benchmark-all":values+=["--accuracy-contexts",str(registry)]
    args=parser.parse_args(values);cli._validate_runtime(parser,args)
    def spec(kind):
        manifest,binding=manifests[kind]
        return cli.approved_worker(package,manifest,binding,cli._runtime_arguments(args,kind=kind),kind=kind)
    def load(value):
        return cli.approved_worker(value["package_root"],value["manifest"],value["binding"],
                                  cli._runtime_arguments(args,kind=value["worker_kind"]),kind=value["worker_kind"])
    rig.supervisor._spec=spec(initial)
    rig.controller.specification_loader=load
    return SimpleNamespace(package=package,registry=registry,args=args,manifests=manifests,spec=spec,files=hashes)


def ready(rig,pid,kind):
    rows=[r for r in observations(rig) if r["pid"]==pid]
    if kind=="benchmark-all":
        return any(r.get("phase")=="accuracy_registry" for r in rows) and any("/benchmark-accuracy/campaigns" in r.get("path","") for r in rows)
    return any(r.get("path")=="/v1/projects/p/benchmarks" for r in rows)


def replace(rig, prepared, kind):
    path,binding=prepared.manifests[kind]
    return rig.controller.command({"operation":"use_spec","worker_kind":kind,
        "package_root":str(prepared.package),"manifest":str(path),"binding":binding})


def assert_locked(path):
    with pytest.raises(BlockingIOError):
        Journal(path)


def test_registered_mixed_child_downgrade_upgrade_retains_auth_journal_and_registry(cli_rig,tmp_path):
    rig=cli_rig;prepared=registered(rig,tmp_path)
    original=json.loads(rig.identity_before)
    registry_before=prepared.registry.read_bytes()
    first=rig.controller.start()
    pids=[first["worker_pid"]]
    wait_for(lambda:ready(rig,pids[-1],"benchmark-all"))
    assert_locked(prepared.args.journal)
    processes=[]
    for kind in ("benchmark-v1","benchmark-all"):
        old=rig.supervisor._process;processes.append(old)
        status=replace(rig,prepared,kind)
        assert status["worker_state"]=="running" and status["controller_error"] is None
        pids.append(status["worker_pid"])
        wait_for(lambda:ready(rig,pids[-1],kind))
        assert old.poll() is not None
        assert_locked(prepared.args.journal)
        assert rig.login==["login"]
        assert rig.broker.running
    assert len(set(pids))==3 and not rig.failures
    entries=[r for r in observations(rig) if r.get("phase")=="entry"]
    assert {r["pid"] for r in entries}==set(pids)
    assert all(r["loaded_cli_path"]==str(prepared.package/"aws_mesouq/benchmark_cli.py") and r["loaded_cli_sha256"]==prepared.files["aws_mesouq/benchmark_cli.py"]
               and r["loaded_dispatch_path"]==str(prepared.package/"aws_mesouq/benchmark_dispatch.py") and r["loaded_dispatch_sha256"]==prepared.files["aws_mesouq/benchmark_dispatch.py"] for r in entries)
    assert rig.fixture.cluster.submit_count==0
    state=json.loads((prepared.args.journal/"journal.json").read_text())
    assert state["archive_device"]==original["archive_device"]
    assert state["owner_sub"]==original["owner_sub"]=="alice"
    assert state["jobs"]["existing-calibration"]=={"retained":True}
    assert state["benchmark_dispatch"]["owner_sub"]=="alice"
    assert state["benchmark_accuracy"]["project_id"]=="p"
    assert prepared.registry.read_bytes()==registry_before
    registry_rows=[r for r in observations(rig) if r.get("phase")=="accuracy_registry"]
    assert {r["pid"] for r in registry_rows}=={pids[0],pids[2]}
    assert all(r["registry_path"]==str(prepared.registry) and r["registry_sha256"]==hashlib.sha256(registry_before).hexdigest() for r in registry_rows)
    if rig.key_before is not None:
        assert hashlib.sha256(rig.key_path.read_bytes()).hexdigest()==rig.key_before


def test_pure_v1_controller_cannot_acquire_registry_authority_by_source_replacement(cli_rig,tmp_path):
    rig=cli_rig;prepared=registered(rig,tmp_path,initial="benchmark-v1")
    first=rig.controller.start()["worker_pid"]
    wait_for(lambda:ready(rig,first,"benchmark-v1"))
    previous=rig.supervisor._process
    status=replace(rig,prepared,"benchmark-all")
    assert status["worker_pid"]==first and previous.poll() is None
    assert status["controller_error"]=="worker_restart_failed"
    assert status["controller_running"] is True and rig.login==["login"]
    assert_locked(prepared.args.journal)
    assert not any(r.get("phase")=="accuracy_registry" for r in observations(rig))
    assert rig.fixture.cluster.submit_count==0
