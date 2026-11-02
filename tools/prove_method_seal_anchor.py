"""Allocated synthetic proof of the native method-seal source/wire contract.

No live campaign/context is loaded. Genuine source custody/admission/seal validators
run against fresh toy fixtures. Only numeric fixtures and external transport I/O
are substituted. Scheduler accounting for this proof remains separate.
"""
from __future__ import annotations
import argparse
import ast
from copy import deepcopy
import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import socket
import time


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def unsealed_fixture(module, source):
    # Reuse the actual established setup, stopping immediately before sealing so
    # the real adapter dispatch performs the first source commit under test.
    tree = ast.parse(source)
    fn = deepcopy(next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "genuine_admission"))
    index = next(i for i,n in enumerate(fn.body) if isinstance(n,ast.Assign)
        and isinstance(n.value,ast.Call) and isinstance(n.value.func,ast.Attribute)
        and n.value.func.attr == "seal_method_states")
    fn.name, fn.decorator_list = "build_unsealed_source_fixture", []
    fn.body = fn.body[:index] + ast.parse(
        "data.update(costs=costs, hnpe_wrapper=hnpe, tmcmc_wrapper=tmcmc); return data").body
    namespace = dict(module.__dict__)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[])),
                 str(module.__file__), "exec"), namespace)
    return namespace[fn.name]


def mutate(verified, kind, protocol):
    anchor = verified["accounting_anchor"]
    if kind == "hash": anchor["snapshot_sha256"] = "0"*64
    elif kind == "snapshot":
        anchor["snapshot"]["allocations"][0]["allocated_cpu_seconds"] += 1
        anchor["snapshot_sha256"] = protocol.checksum(anchor["snapshot"])
    elif kind == "membership": anchor["allocation_ids"] = anchor["allocation_ids"][:-1]
    elif kind == "revision": anchor["revision"] += 1
    elif kind == "source_pin": verified["seal_sha256"] = "0"*64


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("science-root", "baseline-adapter", "fixed-adapter", "output-root"):
        parser.add_argument("--"+name, required=True)
    args = parser.parse_args()
    qa_job = os.environ.get("SLURM_JOB_ID", "")
    if not qa_job.isdecimal() or not os.environ.get("SLURMD_NODENAME"):
        raise RuntimeError("genuine source proof requires a native Slurm QA allocation")
    os.umask(0o077)
    output = Path(args.output_root).absolute()
    if output != output.resolve(): raise ValueError("canonical fresh QA output required")
    output.mkdir(mode=0o700, exist_ok=False); output.chmod(0o700)
    import pytest
    from aws_mesouq import benchmark_accuracy_protocol as protocol
    from meso_uq.benchmark_lab import provenance
    from meso_uq.benchmark_lab.accuracy_v2 import custody, runner
    science = Path(args.science_root).resolve()
    fixture_path = science/"tests/test_benchmark_accuracy_v2_runner_regressions.py"
    fixture_raw = fixture_path.read_bytes()
    if hashlib.sha256(fixture_raw).hexdigest() != "ec3e7f9297bda83fc0edf2a9b133561e69210789c6afee2c11debdb4ad96c7c6":
        raise ValueError("reviewed genuine source fixture changed")
    fixtures = load(fixture_path, "genuine_method_seal_source_fixtures")
    build = unsealed_fixture(fixtures, fixture_raw)
    baseline = load(Path(args.baseline_adapter), "baseline_method_seal_adapter")
    fixed = load(Path(args.fixed_adapter), "fixed_method_seal_adapter")
    outcomes = []
    for label, adapter, corruption in [("baseline-postcommit-failure", baseline, None),
        ("fixed-source-success", fixed, None), *[("tampered-"+kind, fixed, kind)
        for kind in ("hash", "snapshot", "membership", "revision", "source_pin")]]:
        root = output/label; root.mkdir(mode=0o700); root.chmod(0o700)
        events, published, rejected = [], [], None
        with pytest.MonkeyPatch.context() as patch:
            data = build(root, patch)
            raw = deepcopy(data["costs"]["snapshot"])
            raw["revision"] += 1
            raw["allocations"].append({"allocation_id":"synthetic-method-seal-control", "attempt":1,
                "method":"overhead", "kind":"metrics", "factor":None, "status":"claimed",
                "slurm_job_id":None, "allocated_gpu_seconds":None, "allocated_cpu_seconds":None,
                "reserved_gpu_seconds":120, "not_submitted":False})
            request, plan = data["request"], data["plan"]
            manifest = {"schema":protocol.MANIFEST_SCHEMA, "campaign_instance_id":plan["campaign_instance_id"],
                "task_id":"synthetic-method-seal-contract", "attempt":1, "kind":"method_seal", "method":"overhead",
                "factor":None, "effort_target":None, "generation":0, "expected_state_sha256":None,
                "request":request, "scientific_request_hash":raw["scientific_request_hash"],
                "transport_request_hash":raw["transport_request_hash"], "context_id":"a"*64,
                "source_binding_sha256":raw["source_binding_sha256"], "policy_sha256":runner.policy.policy_fingerprint(),
                "accounting":raw, "input_ledger_sha256":protocol.checksum(raw),
                "reservation":{"allocation_id":"synthetic-method-seal-control","gpu_seconds":120,"native_cpus":16,"native_gpus":1},
                "pins":{key:None for key in protocol.PINS},
                "inputs":{"method_wrappers":{m:{"directory":str(root/m),"sha256":"1"*64} for m in ("hnpe","tmcmc")}}}
            protocol.validate_manifest(manifest)
            now = time.time()
            clock = {"slurm_job_id":"99000", "allocation_start_epoch":now, "allocation_deadline_epoch":now+120}
            control = {"manifest":manifest,"control_directory":str(root/"control"),"window":{"internal_seconds":120}}
            expected = protocol.bind_current_allocation(manifest,"99000")
            patch.setattr(provenance,"require_allocation",lambda:"99000")
            patch.setattr(adapter,"_guide",lambda *a:(data["guide"],plan,{"sha256":data["guide_sha"]}))
            wrappers = {"hnpe":data["hnpe_wrapper"],"tmcmc":data["tmcmc_wrapper"]}
            def pinned(control, descriptor, filename, **kwargs):
                if filename == "method-state.json":
                    return deepcopy(wrappers[Path(descriptor["directory"]).name])
                if filename == "method-seal.json":
                    return runner._pinned_json(descriptor["directory"],filename,descriptor["sha256"])
                raise AssertionError("unexpected external transport read")
            patch.setattr(adapter,"_pinned",pinned)
            patch.setattr(adapter,"_native_publication",lambda *a,**k:published.append(deepcopy(k)))
            original_commit = custody.commit_seal
            def commit(*a,**k):
                result = original_commit(*a,**k)
                events.append("genuine_source_commit_completed")
                return result
            patch.setattr(custody,"commit_seal",commit)
            original_validate = runner.validate_sealing_receipt
            def validate(*a,**k):
                verified = original_validate(*a,**k)
                caller = inspect.currentframe().f_back
                if corruption and caller.f_code.co_name == "_dispatch" and caller.f_globals.get("__file__") == adapter.__file__:
                    verified=deepcopy(verified);mutate(verified,corruption,protocol)
                return verified
            patch.setattr(runner,"validate_sealing_receipt",validate)
            try: adapter._dispatch(control,clock)
            except adapter.NativeContractError as error: rejected=type(error).__name__
            if not events: raise AssertionError("source did not commit before adapter boundary")
            if label == "fixed-source-success":
                if rejected or len(published)!=1: raise AssertionError("fixed genuine source dispatch failed")
                record=published[0]
                if record["outputs"]["sealed_allocation_anchor"] != expected: raise AssertionError("raw transport snapshot changed")
                seal=record["outputs"]["method_seal"]
                manifest["inputs"].update(method_seal=seal,sealed_allocation_anchor=expected)
                manifest["pins"].update(record["pins"])
                adapter._sealed(control,custody,runner,plan)
                events.append("genuine_source_consumer_verified")
            elif rejected != "NativeContractError" or published:
                raise AssertionError("expected postcommit rejection was absent")
            seal_path=data["guide"]/"method-seal.json"
            outcomes.append({"case":label,"outcome":"PASS","events":events,"adapter_rejection":rejected,
                "source_seal_sha256":hashlib.sha256(seal_path.read_bytes()).hexdigest(),
                "synthetic_snapshot_sha256":protocol.checksum(expected),"publication_records":len(published)})
    receipt={"schema":"mesouq.method_seal_anchor.genuine_source_proof.v1", "outcome":"PASS",
        "actual_qa_slurm_job_id":qa_job,"actual_qa_node":socket.gethostname(),
        "case_job_ids_and_costs":"synthetic protocol fixtures, not scheduler accounting",
        "current_campaign_or_context_loaded":False,"real_source_validators_preserved":True,
        "fixture_setup":"reviewed genuine_admission setup stopped before first seal; dispatch performs first commit",
        "numerical_work":"established toy fixture substitutions only; no inference/reference generation",
        "source_fixture_sha256":hashlib.sha256(fixture_raw).hexdigest(),
        "baseline_adapter_sha256":hashlib.sha256(Path(args.baseline_adapter).read_bytes()).hexdigest(),
        "fixed_adapter_sha256":hashlib.sha256(Path(args.fixed_adapter).read_bytes()).hexdigest(),"cases":outcomes}
    raw=json.dumps(receipt,sort_keys=True,separators=(",",":"),allow_nan=False).encode()+b"\n"
    path=output/"proof.json"
    with path.open("xb") as handle:
        os.chmod(path,0o600);handle.write(raw);handle.flush();os.fsync(handle.fileno())
    print(json.dumps({"outcome":"PASS","case_count":len(outcomes),"proof_sha256":hashlib.sha256(raw).hexdigest(),"actual_qa_slurm_job_id":qa_job},sort_keys=True))


if __name__ == "__main__":
    main()
