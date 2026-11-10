"""Actual old validators and unchanged native adapter with future submission metadata."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import subprocess,types,sys
import pytest
from aws_mesouq import benchmark_accuracy_protocol as p

ROOT=Path(__file__).parents[1]
spec=importlib.util.spec_from_file_location("fenced_native_fixtures",Path(__file__).with_name("test_benchmark_accuracy_adapter.py"))
fixtures=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixtures)
native=fixtures.native

def future(tmp_path,monkeypatch):
    _portable_xattr_mock_symbols(monkeypatch)
    _,manifest,control,_,reached=fixtures.real_protocol_native_control(tmp_path,monkeypatch,"future-λ")
    context=deepcopy(control["context"])
    context["capabilities"]=sorted(context["capabilities"]+[p.INVOCATION_PROTOCOL])
    context["native_root"]=str(tmp_path/"future-native")
    context["archive_root"]=str(tmp_path/"future-project")
    context["id"]=p.context_id(context)
    manifest=deepcopy(manifest)
    manifest["context_id"]=context["id"]
    manifest["submission_protocol"]=p.INVOCATION_PROTOCOL
    class Guard:
        def __init__(self):self.installed=False;self.context=context
        def install(self):
            self.installed=True
            package=types.ModuleType("meso_uq.benchmark_lab.accuracy_v2")
            policy=types.ModuleType(package.__name__+".policy")
            policy.policy_fingerprint=lambda:"e"*64
            package.policy=policy
            for name in ("meso_uq","meso_uq.benchmark_lab"):
                parent=types.ModuleType(name);parent.__path__=[]
                monkeypatch.setitem(sys.modules,name,parent)
            monkeypatch.setitem(sys.modules,package.__name__,package)
            monkeypatch.setitem(sys.modules,policy.__name__,policy)
            return self
        def check(self):pass
        def close(self):pass
    monkeypatch.setattr(native,"_verify_profile",lambda *a,**kw:Guard())
    claim={"task":{"manifest":manifest,"manifest_sha256":p.checksum(manifest),"marker":control["marker"]}}
    return context,manifest,control,claim,reached


@pytest.mark.parametrize("ref",["0369bfa6cad62517a4e01dcbaee0207c2955a2a5","ede35e7d0cfa39bf9ec6dca0af27fbfd0c6bf44f"])
def test_actual_old_validator_accepts_legacy_but_rejects_future_manifest(ref,tmp_path,monkeypatch):
    context,manifest,control,claim,reached=future(tmp_path,monkeypatch)
    raw = _legacy_source(ref, 'src/aws_mesouq/benchmark_accuracy_protocol.py')
    namespace={"__name__":"aws_mesouq.old_protocol_fixture","__package__":"aws_mesouq","__file__":str(ROOT/"src/aws_mesouq/benchmark_accuracy_protocol.py")}
    exec(compile(raw,namespace["__file__"],"exec",dont_inherit=True),namespace)
    legacy=deepcopy(manifest);legacy.pop("submission_protocol")
    assert namespace["validate_manifest"](legacy)==legacy
    with pytest.raises(ValueError):namespace["validate_manifest"](manifest)
    assert p.checksum(legacy)==namespace["checksum"](legacy)


def test_unchanged_native_adapter_prepares_and_validates_future_manifest(tmp_path,monkeypatch):
    context,manifest,control,claim,reached=future(tmp_path,monkeypatch)
    preparation=native._prepare({"context":context,"operator":control["operator"],
        "claim":claim,"window":control["window"]})
    path=Path(preparation["script_path"]).parent/"control.json"
    loaded=native._load_control(str(path))
    assert loaded["manifest"]["submission_protocol"]==p.INVOCATION_PROTOCOL
    p.validate_manifest_context(loaded["manifest"],context)
    assert native._allocated(loaded,execute=True)==0
    assert reached==["future-λ"]


@pytest.mark.parametrize("which",["missing_flag","missing_capability","unknown_flag"])
def test_mixed_context_or_protocol_metadata_rejects(which,tmp_path,monkeypatch):
    context,manifest,control,claim,reached=future(tmp_path,monkeypatch)
    if which=="missing_flag":manifest.pop("submission_protocol")
    elif which=="missing_capability":
        context["capabilities"].remove(p.INVOCATION_PROTOCOL)
        context["id"]=p.context_id(context);manifest["context_id"]=context["id"]
    else:manifest["submission_protocol"]="untrusted"
    with pytest.raises(ValueError):p.validate_manifest_context(manifest,context)


def _portable_xattr_mock_symbols(monkeypatch):
    import errno
    import os
    def no_linux_acl(*args, **kwargs):
        raise OSError(getattr(errno, "ENODATA", 61), "synthetic POSIX ACL absent")
    for name in ("getxattr", "removexattr"):
        if not hasattr(os, name):
            monkeypatch.setattr(os, name, no_linux_acl, raising=False)


def _legacy_source(ref, relative):
    import importlib.util
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    path = root / "tests" / "fixtures" / "accuracy_legacy_protocol" / "loader.py"
    spec = importlib.util.spec_from_file_location("historical_accuracy_source_fixtures", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.load_source(root, ref, relative)


def test_native_future_manifest_with_absent_platform_xattrs(tmp_path, monkeypatch):
    import os
    for name in ("getxattr", "setxattr", "listxattr", "removexattr"):
        monkeypatch.delattr(os, name, raising=False)
    test_unchanged_native_adapter_prepares_and_validates_future_manifest(tmp_path, monkeypatch)


def test_historical_source_fixture_authentication_without_git(tmp_path):
    import shutil
    fixture_root = Path(__file__).resolve().parents[1]
    source = fixture_root / "tests" / "fixtures" / "accuracy_legacy_protocol"
    target = tmp_path / "tests" / "fixtures" / "accuracy_legacy_protocol"
    shutil.copytree(source, target)
    spec = importlib.util.spec_from_file_location("isolated_historical_sources", target / "loader.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert not (tmp_path / ".git").exists()
    for (ref, relative), (name, sha, blob, size) in module.SOURCES.items():
        assert len(module.load_source(tmp_path, ref, relative)) == size
    ref, relative = next(iter(module.SOURCES))
    name = module.SOURCES[(ref, relative)][0]
    path = target / name
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="fixture integrity"):
        module.load_source(tmp_path, ref, relative)
