"""Native archive helpers and recovery, using synthetic private fixtures."""
from __future__ import annotations
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import pytest
from aws_mesouq.benchmark_worker import ArtifactIntegrityError, BenchmarkWorker, TREE_HELPER
from aws_mesouq.connector.archive import ARCHIVE_HELPER
from aws_mesouq.connector.transport import Transport
from test_benchmark_worker import entries, session


def private_file(root, name, blob=b"private-native-fixture"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(blob)
    path.chmod(0o600)
    return path


def source_stat(path):
    info = path.stat()
    return tuple(getattr(info, key) for key in (
        "st_dev", "st_ino", "st_uid", "st_gid", "st_mode", "st_nlink",
        "st_size", "st_mtime_ns", "st_ctime_ns"))


def snapshot(transport, root):
    return json.loads(transport.run([
        transport.staging_python, "-I", "-c", TREE_HELPER, str(root)]))


def transfers(transport, root):
    inventory = snapshot(transport, root)
    proofs = {name: proof for proof in inventory["source_link_groups"] for name in proof["paths"]}
    result = []
    for item in inventory["files"]:
        transfer = {"source_root": str(root), "source": item["path"],
                    "destination": item["path"], "sha256": item["sha256"],
                    "size_bytes": item["size_bytes"]}
        if item["path"] in proofs:
            transfer["source_link_group"] = proofs[item["path"]]
        result.append(transfer)
    return result


@pytest.fixture
def native_files(tmp_path):
    source = tmp_path / "scratch"
    project_qa = os.environ.get("MESOUQ_NATIVE_PROJECT_QA_ROOT")
    if project_qa:
        parent = Path(project_qa)
        info = parent.lstat()
        assert parent.is_absolute() and str(parent) == os.path.realpath(parent)
        assert parent.is_dir() and not parent.is_symlink()
        assert info.st_uid == os.getuid() and info.st_mode & 0o777 == 0o700
        target = parent / tmp_path.name
    else:
        target = tmp_path / "project"
    source.mkdir(mode=0o700)
    target.mkdir(mode=0o700)
    return Transport(local=True), source, target


def test_closed_internal_aliases_materialize_independent_private_files(native_files):
    transport, source, target = native_files
    first = private_file(source, "a.bin")
    second = source / "b.bin"
    os.link(first, second)
    items = transfers(transport, source)
    assert len(items) == 2
    assert items[0]["source_link_group"]["paths"] == ["a.bin", "b.bin"]
    before = {name: source_stat(source / name) for name in ("a.bin", "b.bin")}
    digest = hashlib.sha256(first.read_bytes()).hexdigest()
    transport.archive_copy_batch(str(target), "native-1", items)
    transport.archive_verify_batch(str(target), "native-1", items)
    destinations = [target / "native-1" / name for name in ("a.bin", "b.bin")]
    assert len({(path.stat().st_dev, path.stat().st_ino) for path in destinations}) == 2
    for path in destinations:
        assert path.stat().st_mode & 0o777 == 0o400
        assert path.stat().st_nlink == 1
        assert (path.stat().st_dev, path.stat().st_ino) != before["a.bin"][:2]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert {name: source_stat(source / name) for name in before} == before
    assert hashlib.sha256(first.read_bytes()).hexdigest() == digest


@pytest.mark.parametrize("kind", ["external", "third-link", "symlink", "unsafe-name", "nonprivate-file", "nonprivate-root"])
def test_inventory_rejects_unclosed_or_unsafe_sources(native_files, tmp_path, kind):
    transport, source, _ = native_files
    first = private_file(source, "a.bin")
    if kind == "external":
        os.link(first, tmp_path / "outside.bin")
    elif kind == "third-link":
        os.link(first, source / "b.bin")
        os.link(first, tmp_path / "outside.bin")
    elif kind == "symlink":
        (source / "alias.bin").symlink_to(first)
    elif kind == "unsafe-name":
        first.rename(source / "unsafe name.bin")
    elif kind == "nonprivate-file":
        first.chmod(0o640)
    else:
        source.chmod(0o750)
    with pytest.raises(subprocess.CalledProcessError):
        snapshot(transport, source)


@pytest.mark.parametrize("kind", ["unproven", "stale", "foreign-file", "foreign-root", "wrong-root", "unsafe-proof", "incomplete-proof", "nonprivate", "late-link", "peer-symlink", "duplicate-proof", "omitted-current"])
def test_copy_rejects_unproven_or_changed_alias_groups(native_files, kind):
    transport, source, target = native_files
    first = private_file(source, "a.bin")
    os.link(first, source / "b.bin")
    item = copy.deepcopy(transfers(transport, source)[0])
    proof = item["source_link_group"]
    if kind == "unproven":
        item.pop("source_link_group")
    elif kind == "stale":
        info = first.stat()
        os.utime(first, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000000))
    elif kind == "foreign-file":
        proof["file"]["uid"] += 1
    elif kind == "foreign-root":
        proof["root"]["uid"] += 1
    elif kind == "wrong-root":
        proof["root"]["ino"] += 1
    elif kind == "unsafe-proof":
        proof["paths"] = ["../outside.bin", "a.bin"]
    elif kind == "incomplete-proof":
        proof["paths"] = ["a.bin"]
    elif kind == "late-link":
        os.link(first, source / "late.bin")
    elif kind == "peer-symlink":
        (source / "b.bin").unlink()
        (source / "b.bin").symlink_to(first)
    elif kind == "duplicate-proof":
        proof["paths"] = ["a.bin", "a.bin"]
    elif kind == "omitted-current":
        proof["paths"] = ["b.bin", "c.bin"]
    else:
        first.chmod(0o640)
    before = source_stat(first)
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        transport.archive_copy_batch(str(target), "native-1", [item])
    assert not (target / "native-1" / "a.bin").exists()
    assert source_stat(first) == before


def test_destination_hardlinks_reject_without_repair_or_source_mutation(native_files):
    transport, source, target = native_files
    original = private_file(source, "a.bin")
    item = transfers(transport, source)[0]
    transport.archive_copy_batch(str(target), "native-1", [item])
    destination = target / "native-1" / "a.bin"
    outside = target / "destination-alias.bin"
    os.link(destination, outside)
    source_before, destination_before = source_stat(original), source_stat(destination)
    for operation in (transport.archive_copy_batch, transport.archive_verify_batch):
        with pytest.raises(subprocess.CalledProcessError):
            operation(str(target), "native-1", [item])
    assert source_stat(original) == source_before
    assert source_stat(destination) == destination_before
    assert outside.read_bytes() == original.read_bytes()


def invalid_batches(item):
    return [
        [], [dict(item, destination=f"file-{i}.bin") for i in range(33)],
        [item, copy.deepcopy(item)],
        [dict(item, destination="../escape.bin")],
        [dict(item, source="../escape.bin")],
        [dict(item, source_root="relative")],
        [dict(item, sha256="not-a-digest")],
        [dict(item, size_bytes=True)], [dict(item, size_bytes=-1)],
        [dict(item, size_bytes=1.0)], [dict(item, unexpected=True)],
        [dict(item, source_link_group=[])],
        [dict(item, source_link_group={"padding": "x" * 4194304})],
    ]


def test_invalid_batches_make_no_rpc_or_target_changes(native_files, monkeypatch):
    transport, source, target = native_files
    private_file(source, "a.bin")
    item = transfers(transport, source)[0]
    def unexpected_rpc(*args, **kwargs):
        pytest.fail("Invalid batch reached a transport RPC")
    monkeypatch.setattr(transport, "run", unexpected_rpc)
    for batch in invalid_batches(item):
        with pytest.raises(ValueError):
            transport.archive_copy_batch(str(target), "native-1", batch)
    assert list(target.iterdir()) == []


def test_remote_batch_validation_precedes_target_mutation(native_files):
    transport, source, target = native_files
    private_file(source, "a.bin")
    item = transfers(transport, source)[0]
    payloads = [b"not-json", b"{}"] + [json.dumps(batch).encode() for batch in invalid_batches(item)]
    for payload in payloads:
        with pytest.raises(subprocess.CalledProcessError):
            transport.run([transport.staging_python, "-I", "-c", ARCHIVE_HELPER,
                           str(target), "native-1", "copy-batch"], data=payload)
        assert list(target.iterdir()) == []


def test_batch_transport_uses_json_stdin(native_files, monkeypatch):
    transport, source, target = native_files
    private_file(source, "a.bin")
    items = transfers(transport, source)
    calls = []
    def capture(argv, **kwargs):
        calls.append((argv, kwargs))
        return "{}"
    monkeypatch.setattr(transport, "run", capture)
    transport.archive_copy_batch(str(target), "native-1", items)
    transport.archive_verify_batch(str(target), "native-1", items)
    assert [argv[-1] for argv, _ in calls] == ["copy-batch", "verify-batch"]
    for argv, kwargs in calls:
        assert argv[:3] == [transport.staging_python, "-I", "-c"]
        assert len(argv) == 7
        assert json.loads(kwargs["data"]) == items
        assert len(kwargs["data"]) <= 4194304


def test_worker_batch_count_byte_caps_and_order():
    limit = 128 * 1024 * 1024
    for sizes in [[0] * 65, [limit // 2, limit // 2, 0, limit + 1, 1]]:
        items = [{"path": f"file-{i}", "size_bytes": size} for i, size in enumerate(sizes)]
        batches = BenchmarkWorker._archive_batches(items)
        assert [item for batch in batches for item in batch] == items
        for batch in batches:
            assert 1 <= len(batch) <= 32
            assert len(batch) == 1 or sum(item["size_bytes"] for item in batch) <= limit
    assert [len(batch) for batch in BenchmarkWorker._archive_batches([
        {"size_bytes": 0} for _ in range(65)])] == [32, 32, 1]
    for invalid in (-1, True, 1.0, None):
        with pytest.raises(ArtifactIntegrityError):
            BenchmarkWorker._archive_batches([{"size_bytes": 0}, {"size_bytes": invalid}])


def test_verify_batch_checks_corruption_after_missing_without_source(native_files):
    transport, source, target = native_files
    private_file(source, "a.bin")
    private_file(source, "b.bin")
    items = transfers(transport, source)
    transport.archive_copy_batch(str(target), "native-1", [items[1]])
    destination = target / "native-1" / "b.bin"
    destination.chmod(0o600)
    destination.write_bytes(b"!" * items[1]["size_bytes"])
    destination.chmod(0o400)
    shutil.rmtree(source)
    before = source_stat(destination)
    with pytest.raises(subprocess.CalledProcessError) as caught:
        transport.archive_verify_batch(str(target), "native-1", items)
    assert caught.value.returncode == 1
    assert source_stat(destination) == before
    destination.unlink()
    with pytest.raises(subprocess.CalledProcessError) as caught:
        transport.archive_verify_batch(str(target), "native-1", items)
    assert caught.value.returncode == 3


def pending_archive(s, *, extra=0, aliases=False):
    s.worker.sweep()
    first = entries(s)[0]
    s.app.handle("POST", f"/v1/projects/p/benchmarks/{s.campaign['id']}/cancel", {"sub": "alice"}, {})
    s.worker.sweep()
    s.worker.sweep()
    root = Path(first["root"])
    for index in range(extra):
        private_file(root, f"retained/native-{index:04d}.bin")
    if aliases:
        original = private_file(root, "retained/linked-a.bin")
        os.link(original, root / "retained/linked-b.bin")
    s.worker.client.request_archive("p", s.campaign["id"])
    campaign = s.worker.client.get("p", s.campaign["id"])["campaign"]
    return campaign, root


def archive_record(s):
    return s.worker.state["campaigns"][s.campaign["id"]]


def assert_plain_archive_cache(record):
    assert all(set(item) == {"path", "sha256", "size_bytes"} for item in record["archive_files"])
    manifest = json.loads(record["archive_manifest"])
    assert all(set(item) == {"path", "sha256", "size_bytes"} for item in manifest["retained_outputs"])
    assert "source_link_group" not in record["archive_manifest"]


def forbid_source_work(s, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Recovery touched source or attempted archive writes")
    monkeypatch.setattr(s.worker, "_archive_snapshot", forbidden)
    monkeypatch.setattr(s.worker, "_immutable", forbidden)
    monkeypatch.setattr(s.cluster, "archive_prepare", forbidden)
    monkeypatch.setattr(s.cluster, "archive_copy_batch", forbidden)


def seed_interrupted_archive(s, campaign, monkeypatch, *, partial):
    original = s.cluster.archive_copy_batch
    def interrupted(root, token, items):
        original(root, token, items[:1] if partial else items)
        if partial or any(item["destination"] == "archive-manifest.json" for item in items):
            raise OSError("Fixture archive copy committed; ACK lost")
    monkeypatch.setattr(s.cluster, "archive_copy_batch", interrupted)
    with pytest.raises(OSError):
        s.worker.archive(campaign)
    monkeypatch.setattr(s.cluster, "archive_copy_batch", original)
    assert not archive_record(s).get("archive_verified")
    assert_plain_archive_cache(archive_record(s))


def test_partial_restart_refreshes_proofs_and_keeps_public_metadata_plain(session, monkeypatch):
    campaign, root = pending_archive(session, extra=40, aliases=True)
    original_snapshot = session.worker._archive_snapshot
    snapshots = []
    def capture_snapshot(path):
        result = original_snapshot(path)
        snapshots.append(copy.deepcopy(result[1]))
        return result
    monkeypatch.setattr(session.worker, "_archive_snapshot", capture_snapshot)
    seed_interrupted_archive(session, campaign, monkeypatch, partial=True)
    record = archive_record(session)
    old_files, old_manifest = copy.deepcopy(record["archive_files"]), record["archive_manifest"]
    alias = root / "retained/linked-a.bin"
    info = alias.stat()
    os.utime(alias, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000000))
    before = {name: source_stat(root / name) for name in ("retained/linked-a.bin", "retained/linked-b.bin")}
    original_copy = session.cluster.archive_copy_batch
    copied_proofs = []
    def capture_copy(target, token, items):
        copied_proofs.extend(copy.deepcopy(item["source_link_group"]) for item in items if "source_link_group" in item)
        return original_copy(target, token, items)
    monkeypatch.setattr(session.cluster, "archive_copy_batch", capture_copy)
    session.worker.archive(campaign)
    assert len(snapshots) == 2
    assert snapshots[0]["retained/linked-a.bin"]["file"] != snapshots[1]["retained/linked-a.bin"]["file"]
    assert copied_proofs and all(proof == snapshots[1]["retained/linked-a.bin"] for proof in copied_proofs)
    assert {name: source_stat(root / name) for name in before} == before
    assert record["archive_files"] == old_files
    assert record["archive_manifest"] == old_manifest
    assert_plain_archive_cache(record)
    assert record["archive_verified"]
    assert session.store.state["benchmark_campaigns"][campaign["id"]]["archive"]["status"] == "verified"


def test_full_target_lost_copy_ack_recovers_after_source_is_gone(session, monkeypatch):
    campaign, root = pending_archive(session, extra=40, aliases=True)
    seed_interrupted_archive(session, campaign, monkeypatch, partial=False)
    shutil.rmtree(root)
    forbid_source_work(session, monkeypatch)
    session.worker.archive(campaign)
    assert archive_record(session)["archive_verified"]
    assert session.store.state["benchmark_campaigns"][campaign["id"]]["archive"]["status"] == "verified"
    assert_plain_archive_cache(archive_record(session))


def test_partial_target_with_source_gone_rejects_before_any_write(session, monkeypatch):
    campaign, root = pending_archive(session, extra=40)
    seed_interrupted_archive(session, campaign, monkeypatch, partial=True)
    shutil.rmtree(root)
    def forbidden(*args, **kwargs):
        pytest.fail("Missing source allowed archive writes")
    monkeypatch.setattr(session.worker, "_immutable", forbidden)
    monkeypatch.setattr(session.cluster, "archive_prepare", forbidden)
    monkeypatch.setattr(session.cluster, "archive_copy_batch", forbidden)
    with pytest.raises(ArtifactIntegrityError):
        session.worker.archive(campaign)
    assert not archive_record(session).get("archive_verified")
    assert session.store.state["benchmark_campaigns"][campaign["id"]]["archive"]["status"] == "requested"


def corrupt_private_target(path):
    size = path.stat().st_size
    path.chmod(0o600)
    path.write_bytes(b"!" * size)
    path.chmod(0o400)


def test_corrupt_target_with_source_gone_rejects_without_source_work(session, monkeypatch):
    campaign, root = pending_archive(session, extra=40)
    seed_interrupted_archive(session, campaign, monkeypatch, partial=False)
    item = archive_record(session)["archive_files"][-1]
    destination = Path(session.operator["archive_root"]) / campaign["id"] / item["path"]
    corrupt_private_target(destination)
    shutil.rmtree(root)
    before = source_stat(destination)
    forbid_source_work(session, monkeypatch)
    with pytest.raises(ArtifactIntegrityError):
        session.worker.archive(campaign)
    assert source_stat(destination) == before
    assert not archive_record(session).get("archive_verified")


def test_missing_first_batch_cannot_hide_later_corruption(session, monkeypatch):
    campaign, root = pending_archive(session, extra=70)
    seed_interrupted_archive(session, campaign, monkeypatch, partial=False)
    files = archive_record(session)["archive_files"]
    batches = session.worker._archive_batches(files)
    assert len(batches) >= 3
    archive = Path(session.operator["archive_root"]) / campaign["id"]
    (archive / files[0]["path"]).unlink()
    corrupt_private_target(archive / files[-1]["path"])
    original_verify = session.cluster.archive_verify_batch
    verified = []
    def capture_verify(target, token, items):
        verified.append([item["destination"] for item in items])
        return original_verify(target, token, items)
    monkeypatch.setattr(session.cluster, "archive_verify_batch", capture_verify)
    forbid_source_work(session, monkeypatch)
    with pytest.raises(ArtifactIntegrityError):
        session.worker.archive(campaign)
    assert len(verified) == len(batches)
    assert verified[-1][-1] == files[-1]["path"]
    assert not archive_record(session).get("archive_verified")


def test_partial_cached_archive_rejects_changed_source_bytes_before_writes(session, monkeypatch):
    campaign, root = pending_archive(session, aliases=True)
    seed_interrupted_archive(session, campaign, monkeypatch, partial=True)
    record = archive_record(session)
    before_files = copy.deepcopy(record["archive_files"])
    before_manifest = record["archive_manifest"]
    (root / "retained/linked-a.bin").write_bytes(b"changed-private-native-fixture")
    def forbidden(*args, **kwargs):
        pytest.fail("Changed source allowed archive writes")
    monkeypatch.setattr(session.worker, "_immutable", forbidden)
    monkeypatch.setattr(session.cluster, "archive_prepare", forbidden)
    monkeypatch.setattr(session.cluster, "archive_copy_batch", forbidden)
    with pytest.raises(ArtifactIntegrityError):
        session.worker.archive(campaign)
    assert record["archive_files"] == before_files
    assert record["archive_manifest"] == before_manifest
    assert not record.get("archive_verified")
    assert_plain_archive_cache(record)


def test_cached_verification_transport_failure_propagates_without_source_work(session, monkeypatch):
    campaign, _ = pending_archive(session)
    seed_interrupted_archive(session, campaign, monkeypatch, partial=True)
    def disconnected(*args, **kwargs):
        raise subprocess.CalledProcessError(255, ["ssh", "fixture"])
    monkeypatch.setattr(session.cluster, "archive_verify_batch", disconnected)
    forbid_source_work(session, monkeypatch)
    with pytest.raises(subprocess.CalledProcessError) as caught:
        session.worker.archive(campaign)
    assert caught.value.returncode == 255
    assert not archive_record(session).get("archive_verified")
