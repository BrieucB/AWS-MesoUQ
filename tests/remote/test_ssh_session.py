"""Synthetic SSH governance checks; no Karolina handshakes are made."""
import io
import json
import multiprocessing
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import pytest
from aws_mesouq.connector import ssh_session as sessions

@pytest.fixture
def tmp_path():
    # macOS runner TMPDIR can exceed the Unix socket limit before this test starts.
    with tempfile.TemporaryDirectory(prefix="mesouq-mux-", dir=Path("/tmp").resolve()) as directory:
        yield Path(directory)


class Clock:
    def __init__(self, value=10000.0):
        self.value = value
    def __call__(self):
        return self.value

class Master:
    def __init__(self, path, *, failed=False):
        self.pid = os.getpid()
        self.returncode = 255 if failed else None
        self.stderr = io.BytesIO(b"ssh: connect to host fake port 22: Connection timed out\n" if failed else b"")
        self.path = path
        self.socket = None
        if not failed:
            self.socket = socket.socket(socket.AF_UNIX)
            self.socket.bind(str(path))
            os.chmod(path, 0o600)
    def poll(self):
        return self.returncode
    def terminate(self):
        self.returncode = 0
        if self.socket is not None:
            self.socket.close()
            self.socket = None
        self.path.unlink(missing_ok=True)
    def kill(self):
        self.terminate()
        self.returncode = -9
    def wait(self, timeout=None):
        return self.returncode

class FakeSSH:
    def __init__(self):
        self.masters = []
        self.rpc_codes = []
        self.failed_start = False
        self.config_calls = 0
        self.admission_seen_before_launch = []
        self.master_commands = []
        self.resolutions = []
        self.configuration = {
            'hostname': ['fake.example'], 'user': ['ownuser'], 'port': ['22'],
            'addressfamily': ['any'], 'canonicalizehostname': ['false'],
            'checkhostip': ['no'], 'verifyhostkeydns': ['false'],
            'gssapiauthentication': ['no'],
            'identityfile': ['~/.ssh/id_ed25519', '~/.ssh/id_rsa'],
            'userknownhostsfile': ['~/.ssh/known_hosts ~/.ssh/known_hosts2'],
            'globalknownhostsfile': ['/etc/ssh/ssh_known_hosts /etc/ssh/ssh_known_hosts2'],
        }
        self.numeric_overrides = {}
        self.addresses = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('192.0.2.1', 22))]
    def popen(self, command, **kwargs):
        self.master_commands.append(list(command))
        assert "-M" in command and "-N" in command
        assert "-oConnectionAttempts=1" in command
        assert "-oConnectTimeout=12" in command
        assert "-oStrictHostKeyChecking=yes" in command
        assert "-oForwardAgent=no" in command
        assert "-oProxyCommand=none" in command
        assert kwargs["umask"] == 0o077
        path = Path(command[command.index("-S") + 1])
        state = json.loads((path.parent / "attempts.json").read_text())
        self.admission_seen_before_launch.append(len(state["attempts"]))
        assert state["active"]["ready"] is False
        endpoint = state['active']['endpoint']
        assert '-oHostname=' + endpoint['address'] in command
        assert '-oAddressFamily=' + endpoint['family'] in command
        master = Master(path, failed=self.failed_start)
        self.masters.append(master)
        return master
    def run(self, command, **kwargs):
        if "-G" in command:
            self.config_calls += 1
            values = {key: list(items) for key, items in self.configuration.items()}
            numeric = False
            for argument in command:
                if not argument.startswith('-o'):
                    continue
                key, separator, value = argument[2:].partition('=')
                key = key.lower()
                if separator and key in {'hostname', 'addressfamily', 'hostkeyalias'}:
                    values[key] = [value]
                    numeric = numeric or key == 'hostname'
            if '-l' in command:
                values['user'] = [command[command.index('-l') + 1]]
            if '-p' in command:
                values['port'] = [command[command.index('-p') + 1]]
            if numeric:
                values.update({key: list(items) for key, items in self.numeric_overrides.items()})
            output = ''.join(key + ' ' + value + '\n' for key, items in values.items() for value in items)
            return subprocess.CompletedProcess(command, 0, output.encode(), b'')
        assert "-F" in command and command[command.index("-F") + 1] == "/dev/null"
        assert "-oProxyCommand=/usr/bin/false" in command
        assert "-oProxyJump=none" in command
        if "-O" in command:
            alive = bool(self.masters and self.masters[-1].poll() is None)
            return subprocess.CompletedProcess(command, 0 if alive else 255, b"", b"")
        code = self.rpc_codes.pop(0) if self.rpc_codes else 0
        return subprocess.CompletedProcess(command, code, b"ok", b"remote failed" if code else b"")

@pytest.fixture
def fake_ssh(monkeypatch):
    fake = FakeSSH()
    monkeypatch.setattr(sessions.subprocess, "Popen", fake.popen)
    monkeypatch.setattr(sessions.subprocess, "run", fake.run)
    def getaddrinfo(host, port, family, kind, protocol):
        fake.resolutions.append((host, port, family, kind, protocol))
        return list(fake.addresses)
    monkeypatch.setattr(sessions.socket, "getaddrinfo", getaddrinfo)
    return fake

def test_many_operations_share_one_master_and_preserve_nonzero_control_flow(tmp_path, fake_ssh):
    guard = tmp_path / "guard"
    with sessions.SSHSession("karolina", guard) as owner:
        with sessions.SSHSession("karolina", guard, borrow_only=True) as other:
            for _ in range(12):
                assert owner.run_bytes(["id", "-un"]) == b"ok"
                assert other.run_bytes(["hostname", "-f"]) == b"ok"
            fake_ssh.rpc_codes = [1, 0]
            with pytest.raises(subprocess.CalledProcessError) as error:
                owner.run_bytes(["test", "-f", "missing"])
            assert error.value.returncode == 1
            assert owner.snapshot()["circuit"] is None
            assert other.run_bytes(["true"]) == b"ok"
        assert fake_ssh.masters[0].poll() is None
    assert len(fake_ssh.masters) == 1
    assert fake_ssh.admission_seen_before_launch == [1]
    assert json.loads((guard / "attempts.json").read_text())["active"] is None

def test_exact_rolling_budget_survives_clean_sessions_and_denies_before_launch(tmp_path, fake_ssh):
    clock, guard = Clock(), tmp_path / "guard"
    for offset in (0, 1, 2):
        clock.value = 10000.0 + offset
        with sessions.SSHSession("karolina", guard, clock=clock):
            pass
    clock.value = 10003.0
    before = fake_ssh.config_calls
    with pytest.raises(sessions.SSHSessionError, match="attempt_budget_exhausted"):
        sessions.SSHSession("karolina", guard, clock=clock)
    assert len(fake_ssh.masters) == 3
    assert fake_ssh.config_calls == before
    clock.value = 13600.0
    with sessions.SSHSession("karolina", guard, clock=clock):
        pass
    data = json.loads((guard / "attempts.json").read_text())
    assert data["attempts"] == [10001.0, 10002.0, 13600.0]
    assert data["counters"]["admitted"] == 4

def test_transport_failure_blocks_other_clients_without_launch_or_rpc(tmp_path, fake_ssh):
    guard = tmp_path / "guard"
    with sessions.SSHSession("karolina", guard) as owner:
        with sessions.SSHSession("karolina", guard, borrow_only=True) as other:
            fake_ssh.rpc_codes = [255, 0]
            with pytest.raises(sessions.SSHSessionError, match="connection_lost"):
                owner.run_bytes(["squeue"])
            with pytest.raises(sessions.SSHSessionError, match="connection_lost"):
                other.run_bytes(["sacct"])
            assert fake_ssh.rpc_codes == [0]
    assert len(fake_ssh.masters) == 1
    state = json.loads((guard / "attempts.json").read_text())
    assert state["active"] is not None and state["circuit"] == "connection_lost"
    with pytest.raises(sessions.SSHSessionError):
        sessions.SSHSession("karolina", guard)
    assert len(fake_ssh.masters) == 1

def test_failed_start_counts_and_cannot_automatically_retry(tmp_path, fake_ssh):
    guard = tmp_path / "guard"
    fake_ssh.failed_start = True
    with pytest.raises(sessions.SSHSessionError, match="connect_timeout"):
        sessions.SSHSession("karolina", guard)
    assert fake_ssh.admission_seen_before_launch == [1]
    with pytest.raises(sessions.SSHSessionError, match="connect_timeout"):
        sessions.SSHSession("karolina", guard)
    assert len(fake_ssh.masters) == 1
    state = json.loads((guard / "attempts.json").read_text())
    assert len(state["attempts"]) == 1 and state["active"] is not None

def test_owner_death_tombstone_requires_proven_offline_recovery(tmp_path, fake_ssh, monkeypatch):
    guard = tmp_path / "guard"
    fake_ssh.failed_start = True
    with pytest.raises(sessions.SSHSessionError):
        sessions.SSHSession("karolina", guard)
    with pytest.raises(sessions.SSHSessionError, match="old_process_still_alive"):
        sessions.recover(guard)
    monkeypatch.setattr(sessions, "_alive", lambda pid: False)
    monkeypatch.setattr(sessions, "_check", lambda *args: False)
    sessions.recover(guard)
    state = json.loads((guard / "attempts.json").read_text())
    assert len(state["attempts"]) == 1
    assert state["active"] is None and state["circuit"] is None

def test_unknown_spawn_pid_and_unknown_orphan_socket_are_not_auto_reconciled(tmp_path, monkeypatch):
    guard = tmp_path / "guard"
    store = sessions._Store(guard)
    with store.locked() as (state, now):
        state["active"] = {"id": "a" * 32, "host_alias": "karolina", "owner_pid": 99999999, "master_pid": None, "ready": False, "target": {"hostname": "fake.example", "user": "ownuser", "port": 22}, "endpoint": {"address": "192.0.2.1", "family": "inet"}}
        store.save(state, now)
    monkeypatch.setattr(sessions, "_alive", lambda pid: False)
    with pytest.raises(sessions.SSHSessionError, match="uncertain_master_identity"):
        sessions.recover(guard)
    with store.locked() as (state, now):
        state["active"] = None
        store.save(state, now)
    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.bind(str(store.socket)); os.chmod(store.socket, 0o600)
        with pytest.raises(sessions.SSHSessionError, match="orphaned_control_socket"):
            sessions.recover(guard)
        assert store.socket.exists()
    finally:
        sock.close()

def test_clock_rollback_deleted_ledger_and_unsafe_file_fail_closed(tmp_path, fake_ssh):
    clock, guard = Clock(), tmp_path / "guard"
    with sessions.SSHSession("karolina", guard, clock=clock):
        pass
    clock.value = 9999.0
    with pytest.raises(sessions.SSHSessionError, match="clock_rollback"):
        sessions.SSHSession("karolina", guard, clock=clock)
    clock.value = 10001.0
    (guard / "attempts.json").unlink()
    with pytest.raises(sessions.SSHSessionError, match="unsafe_state"):
        sessions.SSHSession("karolina", guard, clock=clock)
    assert len(fake_ssh.masters) == 1

@pytest.mark.parametrize("bad_kind", ["mode", "symlink", "hardlink", "oversize", "schema_bool", "nonfinite"])
def test_ledger_validation_before_any_connection(tmp_path, fake_ssh, bad_kind):
    guard = tmp_path / "guard"
    store = sessions._Store(guard)
    with store.locked() as (state, now):
        store.save(state, now)
    path = guard / "attempts.json"
    if bad_kind == "mode":
        path.chmod(0o644)
    elif bad_kind == "symlink":
        path.rename(guard / "elsewhere")
        path.symlink_to(guard / "elsewhere")
    elif bad_kind == "hardlink":
        os.link(path, guard / "alias")
    elif bad_kind == "oversize":
        path.write_bytes(b" " * 65537)
    else:
        state = json.loads(path.read_text())
        state["schema" if bad_kind == "schema_bool" else "watermark"] = True if bad_kind == "schema_bool" else float("nan")
        path.write_text(json.dumps(state))
    with pytest.raises((sessions.SSHSessionError, OSError)):
        sessions.SSHSession("karolina", guard)
    assert fake_ssh.masters == []

def test_external_attempt_import_is_append_only_and_consumes_budget(tmp_path, fake_ssh):
    guard, clock = tmp_path / "guard", Clock()
    sessions.record_external_attempt(guard, 9998.0, clock=clock)
    sessions.record_external_attempt(guard, 9999.0, clock=clock)
    with sessions.SSHSession("karolina", guard, clock=clock):
        pass
    with pytest.raises(sessions.SSHSessionError, match="attempt_budget_exhausted"):
        sessions.SSHSession("karolina", guard, clock=clock)
    assert json.loads((guard / "attempts.json").read_text())["attempts"] == [9998.0, 9999.0, 10000.0]
    assert len(fake_ssh.masters) == 1

def _borrow_worker(directory, pipe):
    try:
        with sessions.SSHSession("karolina", directory, borrow_only=True) as client:
            pipe.send({"output": client.run_bytes(["true"]), "owner": client.snapshot()["session_owner"]})
    except Exception as error:
        pipe.send({"error": type(error).__name__ + ":" + str(error)})
    finally:
        pipe.close()

def test_cross_process_clients_share_master(tmp_path, fake_ssh):
    context = multiprocessing.get_context("fork")
    guard = tmp_path / "guard"
    with sessions.SSHSession("karolina", guard) as owner:
        processes, readers = [], []
        for _ in range(4):
            reader, writer = context.Pipe(duplex=False)
            process = context.Process(target=_borrow_worker, args=(guard, writer))
            process.start(); writer.close()
            processes.append(process); readers.append(reader)
        for process, reader in zip(processes, readers):
            assert reader.poll(10)
            assert reader.recv() == {"output": b"ok", "owner": False}
            process.join(10)
            assert process.exitcode == 0
            reader.close()
        assert owner.snapshot()["counters"]["rpc_requests"] == 4
    assert len(fake_ssh.masters) == 1

def _record_worker(directory, pipe):
    try:
        sessions.record_external_attempt(directory, 9999.0, clock=lambda: 10000.0)
        pipe.send("admitted")
    except sessions.SSHSessionError as error:
        pipe.send(error.category)
    finally:
        pipe.close()

def test_simultaneous_processes_cannot_exceed_rolling_limit(tmp_path):
    context = multiprocessing.get_context("fork")
    guard = tmp_path / "guard"
    processes, readers = [], []
    for _ in range(4):
        reader, writer = context.Pipe(duplex=False)
        process = context.Process(target=_record_worker, args=(guard, writer))
        process.start(); writer.close()
        processes.append(process); readers.append(reader)
    results = []
    for process, reader in zip(processes, readers):
        assert reader.poll(10)
        results.append(reader.recv())
        process.join(10)
        assert process.exitcode == 0
        reader.close()
    assert results.count("admitted") == 3
    assert results.count("attempt_budget_exhausted") == 1
    assert len(json.loads((guard / "attempts.json").read_text())["attempts"]) == 3

@pytest.mark.parametrize("die_after_validation", [False, True])
def test_installed_ssh_missing_or_raced_mux_never_connects_tcp(tmp_path, monkeypatch, die_after_validation):
    guard = tmp_path / "guard"
    store = sessions._Store(guard)
    tcp = socket.socket()
    tcp.bind(("127.0.0.1", 0)); tcp.listen(1); tcp.settimeout(0.2)
    mux = socket.socket(socket.AF_UNIX)
    mux.bind(str(store.socket)); os.chmod(store.socket, 0o600)
    target = {"hostname": "127.0.0.1", "user": "nobody", "port": tcp.getsockname()[1]}
    try:
        with store.locked() as (state, now):
            state["active"] = {"id": "b" * 32, "host_alias": "karolina", "owner_pid": os.getpid(), "master_pid": os.getpid(), "ready": True, "target": target, "endpoint": {"address": "127.0.0.1", "family": "inet"}}
            store.save(state, now)
        monkeypatch.setattr(sessions, "_check", lambda *args: True)
        with sessions.SSHSession("karolina", guard, borrow_only=True) as client:
            if die_after_validation:
                original = sessions._client_command
                def disappear_after_validation(*args, **kwargs):
                    mux.close(); store.socket.unlink()
                    return original(*args, **kwargs)
                monkeypatch.setattr(sessions, "_client_command", disappear_after_validation)
            else:
                mux.close()
            with pytest.raises(sessions.SSHSessionError, match="connection_lost"):
                client.run_bytes(["true"])
        with pytest.raises(socket.timeout):
            tcp.accept()
        assert json.loads((guard / "attempts.json").read_text())["circuit"] == "connection_lost"
    finally:
        mux.close(); tcp.close()


def test_corrupt_state_close_still_reaps_owned_master(tmp_path, fake_ssh):
    guard = tmp_path / 'guard'
    owner = sessions.SSHSession('karolina', guard)
    (guard / 'attempts.json').chmod(0o644)
    with pytest.raises(sessions.SSHSessionError, match='unsafe_state'):
        owner.close()
    assert fake_ssh.masters[0].poll() is not None
    assert len(fake_ssh.masters) == 1

def test_borrow_only_and_status_never_launch_master(tmp_path, fake_ssh):
    guard = tmp_path / "guard"
    with pytest.raises(sessions.SSHSessionError, match="no_ready_session"):
        sessions.SSHSession("karolina", guard, borrow_only=True)
    assert fake_ssh.masters == []
    assert sessions.status(guard)["new_connections"] == 0
    assert fake_ssh.config_calls == 0

def test_rpc_timeout_persists_circuit_and_blocks_following_rpc(tmp_path, fake_ssh, monkeypatch):
    guard = tmp_path / "guard"
    with sessions.SSHSession("karolina", guard) as owner:
        original, calls = fake_ssh.run, []
        def timeout(command, **kwargs):
            if "-G" not in command and "-O" not in command:
                calls.append(command)
                raise subprocess.TimeoutExpired(command, 90)
            return original(command, **kwargs)
        monkeypatch.setattr(sessions.subprocess, "run", timeout)
        with pytest.raises(sessions.SSHSessionError, match="command_timeout"):
            owner.run_bytes(["squeue"])
        with pytest.raises(sessions.SSHSessionError, match="command_timeout"):
            owner.run_bytes(["sacct"])
        assert len(calls) == 1
    assert json.loads((guard / "attempts.json").read_text())["circuit"] == "command_timeout"

def test_cleanup_reaps_owned_master_even_if_ledger_is_invalid_json(tmp_path, fake_ssh):
    guard = tmp_path / "guard"
    owner = sessions.SSHSession("karolina", guard)
    (guard / "attempts.json").write_text("not json")
    with pytest.raises(sessions.SSHSessionError):
        owner.close()
    assert fake_ssh.masters[0].poll() is not None

def test_foreground_hold_interrupt_reaps_master_and_preserves_budget(tmp_path, fake_ssh, monkeypatch):
    guard = tmp_path / "guard"
    def stop(seconds):
        raise KeyboardInterrupt
    monkeypatch.setattr(sessions.time, "sleep", stop)
    assert sessions.main(["--state-dir", str(guard), "hold", "--host", "karolina"]) == 130
    state = json.loads((guard / "attempts.json").read_text())
    assert state["active"] is None and state["circuit"] is None
    assert len(state["attempts"]) == 1 and fake_ssh.masters[0].poll() is not None

def _owner_race_worker(directory, start, release, pipe, fake):
    try:
        assert start.wait(10)
        with sessions.SSHSession("karolina", directory) as client:
            pipe.send({"owner": client.snapshot()["session_owner"], "masters": len(fake.masters)})
            assert release.wait(10)
    except sessions.SSHSessionError as error:
        pipe.send({"error": error.category, "masters": len(fake.masters)})
    finally:
        pipe.close()

def test_racing_owner_processes_cannot_create_two_masters(tmp_path, fake_ssh):
    context = multiprocessing.get_context("fork")
    start, release = context.Event(), context.Event()
    guard = tmp_path / "guard"
    processes, readers = [], []
    for _ in range(4):
        reader, writer = context.Pipe(duplex=False)
        process = context.Process(target=_owner_race_worker, args=(guard, start, release, writer, fake_ssh))
        process.start()
        writer.close()
        processes.append(process)
        readers.append(reader)
    try:
        start.set()
        results = []
        for reader in readers:
            assert reader.poll(10)
            results.append(reader.recv())
        assert sum(item["masters"] for item in results) == 1
        assert sum(bool(item.get("owner")) for item in results) == 1
        assert all(item.get("error") in (None, "no_ready_session") for item in results)
    finally:
        release.set()
        for process, reader in zip(processes, readers):
            process.join(10)
            reader.close()
            assert process.exitcode == 0
    assert len(json.loads((guard / "attempts.json").read_text())["attempts"]) == 1


def test_guarded_cli_forwards_input_bytes_without_opening_a_master(tmp_path, monkeypatch, capsys):
    import sys
    received, exited = [], []
    class Borrowed:
        def __init__(self, host, directory, *, borrow_only=False):
            assert host == 'karolina' and borrow_only is True
        def __enter__(self):
            return self
        def __exit__(self, *unused):
            exited.append(True)
        def run_bytes(self, argv, *, data=None):
            received.append((argv, data))
            return b'fixture-output'
    monkeypatch.setattr(sessions, 'SSHSession', Borrowed)
    monkeypatch.setattr(sys, 'stdin', type('Input', (), {'buffer': io.BytesIO(b'bytes\x00\xff')})())
    result = sessions.main(['--state-dir', str(tmp_path/'guard'), 'run', '--host', 'karolina', '--stdin', '--', 'cat'])
    assert result == 0 and received == [(['cat'], b'bytes\x00\xff')]
    assert exited == [True] and capsys.readouterr().out == 'fixture-output'

def test_configuration_failure_is_sanitized_before_admission(tmp_path, fake_ssh, monkeypatch):
    def invalid(command, **kwargs):
        raise subprocess.CalledProcessError(255, command, stderr=b'fixture private configuration detail')
    monkeypatch.setattr(sessions.subprocess, 'run', invalid)
    with pytest.raises(sessions.SSHSessionError, match='configuration'):
        sessions.SSHSession('karolina', tmp_path/'guard')
    assert fake_ssh.masters == []
    assert json.loads((tmp_path/'guard'/'attempts.json').read_text())['attempts'] == []

def test_foreground_hold_sigterm_reaps_owned_master(tmp_path, fake_ssh, monkeypatch):
    import signal
    guard = tmp_path / "guard"
    def terminate(seconds):
        os.kill(os.getpid(), signal.SIGTERM)
    monkeypatch.setattr(sessions.time, "sleep", terminate)
    with pytest.raises(SystemExit) as result:
        sessions.hold("karolina", guard)
    assert result.value.code == 143
    state = json.loads((guard / "attempts.json").read_text())
    assert state["active"] is None and state["circuit"] is None
    assert fake_ssh.masters[0].poll() is not None


def _assert_unadmitted(guard, fake):
    state = json.loads((guard / 'attempts.json').read_text())
    assert state['attempts'] == []
    assert state['active'] is None
    assert fake.masters == []


def test_one_literal_endpoint_is_durable_before_the_only_master_launch(tmp_path, fake_ssh):
    fake_ssh.addresses = [
        (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('2001:db8::1', 22, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('192.0.2.2', 22)),
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('192.0.2.3', 22)),
    ]
    guard = tmp_path / 'guard'
    with sessions.SSHSession('karolina', guard):
        state = json.loads((guard / 'attempts.json').read_text())
        assert state['schema'] == 2
        assert state['active']['target'] == {'hostname': 'fake.example', 'user': 'ownuser', 'port': 22}
        assert state['active']['endpoint'] == {'address': '2001:db8::1', 'family': 'inet6'}
    assert fake_ssh.resolutions == [('fake.example', 22, socket.AF_UNSPEC, socket.SOCK_STREAM, socket.IPPROTO_TCP)]
    assert fake_ssh.config_calls == 2
    assert len(fake_ssh.master_commands) == 1
    command = fake_ssh.master_commands[0]
    assert '-oHostname=2001:db8::1' in command
    assert '-oAddressFamily=inet6' in command
    assert '-oHostKeyAlias=fake.example' in command
    assert '-oCanonicalizeHostname=no' in command
    assert command[-1] == 'karolina'
    assert fake_ssh.admission_seen_before_launch == [1]


@pytest.mark.parametrize('configured,family,address,endpoint', [
    ('inet', socket.AF_INET, ('192.0.2.1', 22), {'address': '192.0.2.1', 'family': 'inet'}),
    ('inet6', socket.AF_INET6, ('2001:db8::1', 22, 0, 0), {'address': '2001:db8::1', 'family': 'inet6'}),
])
def test_resolver_respects_effective_address_family(tmp_path, fake_ssh, configured, family, address, endpoint):
    fake_ssh.configuration['addressfamily'] = [configured]
    fake_ssh.addresses = [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', address)]
    target, selected, command = sessions._resolve('karolina', tmp_path / 'mux')
    assert selected == endpoint
    assert target == {'hostname': 'fake.example', 'user': 'ownuser', 'port': 22}
    assert fake_ssh.resolutions == [('fake.example', 22, family, socket.SOCK_STREAM, socket.IPPROTO_TCP)]
    assert '-oHostname=' + endpoint['address'] in command


@pytest.mark.parametrize('alias', [None, 'trusted-key-name', 'none'])
def test_numeric_pin_preserves_effective_host_key_identity(tmp_path, fake_ssh, alias):
    if alias is not None:
        fake_ssh.configuration['hostkeyalias'] = [alias]
    _, _, command = sessions._resolve('karolina', tmp_path / 'mux')
    assert '-oHostKeyAlias=' + ('fake.example' if alias is None else alias) in command


def test_mixed_case_openssh_dump_keys_are_normalized(tmp_path, fake_ssh):
    fake_ssh.configuration['canonicalizePermittedcnames'] = ['none']
    sessions._resolve('karolina', tmp_path / 'mux')
    assert fake_ssh.config_calls == 2


def test_failed_selected_endpoint_never_launches_another_candidate(tmp_path, fake_ssh):
    fake_ssh.failed_start = True
    fake_ssh.addresses += [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('192.0.2.2', 22))]
    guard = tmp_path / 'guard'
    with pytest.raises(sessions.SSHSessionError):
        with sessions.SSHSession('karolina', guard):
            pytest.fail('failed master became ready')
    assert len(fake_ssh.masters) == 1
    assert len(fake_ssh.resolutions) == 1
    assert len(json.loads((guard / 'attempts.json').read_text())['attempts']) == 1


@pytest.mark.parametrize('key,value', [
    ('port', '2222'), ('addressfamily', 'invalid'),
    ('canonicalizehostname', 'true'), ('canonicalizepermittedcnames', '*.example.com:*'),
    ('knownhostscommand', '/usr/bin/true'), ('localcommand', '/usr/bin/true'),
    ('checkhostip', 'yes'), ('verifyhostkeydns', 'ask'), ('gssapiauthentication', 'yes'),
    ('identityfile', '~/.ssh/%h'), ('certificatefile', '~/.ssh/%h-cert.pub'),
    ('identityagent', '/tmp/agent-%h'), ('userknownhostsfile', '~/.ssh/known_hosts-%h'),
    ('globalknownhostsfile', '/etc/ssh/known_hosts-%h'), ('revokedhostkeys', '/tmp/revoked-%h'),
    ('pkcs11provider', '/tmp/provider-%h'), ('securitykeyprovider', '/tmp/provider-%h'),
])
def test_unsupported_effective_configuration_consumes_no_attempt(tmp_path, fake_ssh, key, value):
    fake_ssh.configuration[key] = [value]
    guard = tmp_path / 'guard'
    with pytest.raises(sessions.SSHSessionError, match='configuration'):
        with sessions.SSHSession('karolina', guard):
            pytest.fail('unsupported configuration was admitted')
    _assert_unadmitted(guard, fake_ssh)


@pytest.mark.parametrize('key,baseline,changed', [
    ('identityfile', ['~/.ssh/id_ed25519', '~/.ssh/id_rsa'], ['~/.ssh/id_ed25519', '~/.ssh/other_key']),
    ('userknownhostsfile', ['~/.ssh/known_hosts ~/.ssh/known_hosts2'], ['/tmp/other_known_hosts']),
    ('pubkeyauthentication', ['yes'], ['no']),
])
def test_numeric_match_auth_or_trust_drift_is_denied_before_admission(tmp_path, fake_ssh, key, baseline, changed):
    fake_ssh.configuration[key] = baseline
    fake_ssh.numeric_overrides[key] = changed
    guard = tmp_path / 'guard'
    with pytest.raises(sessions.SSHSessionError, match='configuration'):
        with sessions.SSHSession('karolina', guard):
            pytest.fail('numeric-host configuration drift was admitted')
    _assert_unadmitted(guard, fake_ssh)
    assert fake_ssh.config_calls == 2
    assert len(fake_ssh.resolutions) == 1


def test_dns_failure_consumes_no_attempt(tmp_path, fake_ssh, monkeypatch):
    calls = []
    def fail_resolution(*arguments):
        calls.append(arguments)
        raise socket.gaierror('synthetic DNS failure')
    monkeypatch.setattr(sessions.socket, 'getaddrinfo', fail_resolution)
    guard = tmp_path / 'guard'
    with pytest.raises(sessions.SSHSessionError, match='configuration'):
        with sessions.SSHSession('karolina', guard):
            pytest.fail('unresolved target was admitted')
    _assert_unadmitted(guard, fake_ssh)
    assert len(calls) == 1


@pytest.mark.parametrize('first', [
    (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('not-a-literal', 22)),
    (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('fe80::1', 22, 0, 3)),
    (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('192.0.2.1', 2222)),
])
def test_unsupported_first_address_does_not_fall_through(tmp_path, fake_ssh, first):
    fake_ssh.addresses = [first, (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('192.0.2.2', 22))]
    guard = tmp_path / 'guard'
    with pytest.raises(sessions.SSHSessionError, match='configuration'):
        with sessions.SSHSession('karolina', guard):
            pytest.fail('unsupported first address was admitted')
    _assert_unadmitted(guard, fake_ssh)
    assert len(fake_ssh.resolutions) == 1
    assert fake_ssh.config_calls == 1


@pytest.mark.parametrize('schema,endpoint', [
    (1, {'address': '192.0.2.1', 'family': 'inet'}),
    (2, None), (2, {}),
    (2, {'address': 'fake.example', 'family': 'inet'}),
    (2, {'address': '2001:db8::1', 'family': 'inet'}),
    (2, {'address': '192.0.2.1', 'family': []}),
    (2, {'address': 'fe80::1%eth0', 'family': 'inet6'}),
    (2, {'address': '192.0.2.1', 'family': 'inet', 'extra': True}),
])
def test_legacy_or_unsafe_endpoint_ledger_is_denied(tmp_path, schema, endpoint):
    guard = tmp_path / 'guard'
    store = sessions._Store(guard)
    path = guard / 'attempts.json'
    state = json.loads(path.read_text())
    state['schema'] = schema
    state['active'] = {
        'id': 'a' * 32, 'host_alias': 'karolina', 'owner_pid': os.getpid(),
        'master_pid': None, 'ready': False,
        'target': {'hostname': 'fake.example', 'user': 'ownuser', 'port': 22},
    }
    if endpoint is not None:
        state['active']['endpoint'] = endpoint
    path.write_text(json.dumps(state))
    with pytest.raises(sessions.SSHSessionError, match='unsafe_state'):
        with store.locked():
            pytest.fail('unsafe endpoint ledger was trusted')


@pytest.mark.parametrize('returncode', [-9, -15])
def test_signaled_mux_client_latches_shared_fatal_circuit(tmp_path, fake_ssh, returncode):
    guard = tmp_path / 'guard'
    with sessions.SSHSession('karolina', guard) as owner:
        with sessions.SSHSession('karolina', guard, borrow_only=True) as borrower:
            fake_ssh.rpc_codes = [returncode, 0]
            with pytest.raises(sessions.SSHSessionError, match='client_error'):
                owner.run_bytes(['squeue'])
            with pytest.raises(sessions.SSHSessionError, match='client_error'):
                borrower.run_bytes(['sacct'])
            assert fake_ssh.rpc_codes == [0]
    state = json.loads((guard / 'attempts.json').read_text())
    assert state['circuit'] == 'client_error'
    assert state['active'] is not None and len(fake_ssh.masters) == 1


@pytest.mark.parametrize('returncode', [1, 127, 254])
def test_positive_remote_nonzero_remains_nonfatal(tmp_path, fake_ssh, returncode):
    with sessions.SSHSession('karolina', tmp_path / 'guard') as owner:
        fake_ssh.rpc_codes = [returncode, 0]
        with pytest.raises(subprocess.CalledProcessError) as failure:
            owner.run_bytes(['remote-probe'])
        assert failure.value.returncode == returncode
        assert owner.snapshot()['circuit'] is None
        assert owner.run_bytes(['true']) == b'ok'


def test_resolution_delay_refreshes_and_reprunes_before_durable_reservation(tmp_path, fake_ssh, monkeypatch):
    clock, guard = Clock(), tmp_path / 'guard'
    sessions.record_external_attempt(guard, 6405.0, clock=clock)
    sessions.record_external_attempt(guard, 6406.0, clock=clock)
    original_run = fake_ssh.run
    original_resolution = sessions.socket.getaddrinfo
    original_popen = fake_ssh.popen
    reservations = []
    def delayed_configuration(command, **kwargs):
        result = original_run(command, **kwargs)
        if '-G' in command:
            clock.value += 2
        return result
    def delayed_resolution(*arguments):
        result = original_resolution(*arguments)
        clock.value += 3
        return result
    def launch(command, **kwargs):
        path = Path(command[command.index('-S') + 1]).parent / 'attempts.json'
        reservations.append(json.loads(path.read_text())['attempts'])
        return original_popen(command, **kwargs)
    monkeypatch.setattr(sessions.subprocess, 'run', delayed_configuration)
    monkeypatch.setattr(sessions.socket, 'getaddrinfo', delayed_resolution)
    monkeypatch.setattr(sessions.subprocess, 'Popen', launch)
    with sessions.SSHSession('karolina', guard, clock=clock):
        pass
    assert reservations == [[10007.0]]
    state = json.loads((guard / 'attempts.json').read_text())
    assert state['attempts'] == [10007.0]
    assert state['counters']['admitted'] == 3 and len(fake_ssh.masters) == 1


@pytest.mark.parametrize('changed_time', [9999.0, float('nan'), float('inf')])
def test_clock_is_revalidated_after_resolution_before_admission(tmp_path, fake_ssh, monkeypatch, changed_time):
    clock, guard = Clock(), tmp_path / 'guard'
    original = sessions._resolve
    def resolve(*arguments):
        result = original(*arguments)
        clock.value = changed_time
        return result
    monkeypatch.setattr(sessions, '_resolve', resolve)
    with pytest.raises(sessions.SSHSessionError, match='clock_rollback'):
        sessions.SSHSession('karolina', guard, clock=clock)
    _assert_unadmitted(guard, fake_ssh)


def test_authenticated_ready_time_conservatively_bounds_rolling_physical_starts(tmp_path, fake_ssh, monkeypatch):
    clock, guard = Clock(), tmp_path / 'guard'
    original = sessions._check
    ready_times = []
    def delayed_ready(*arguments):
        clock.value += 20
        result = original(*arguments)
        if result:
            ready_times.append(clock.value)
        return result
    monkeypatch.setattr(sessions, '_check', delayed_ready)
    for reservation in (10000.0, 10021.0, 10042.0):
        clock.value = reservation
        with sessions.SSHSession('karolina', guard, clock=clock):
            pass
    state = json.loads((guard / 'attempts.json').read_text())
    assert ready_times == [10020.0, 10041.0, 10062.0]
    assert state['attempts'] == ready_times
    clock.value = 13600.0
    with pytest.raises(sessions.SSHSessionError, match='attempt_budget_exhausted'):
        sessions.SSHSession('karolina', guard, clock=clock)
    assert len(fake_ssh.masters) == 3
    clock.value = 13620.0
    with sessions.SSHSession('karolina', guard, clock=clock):
        pass
    state = json.loads((guard / 'attempts.json').read_text())
    assert state['attempts'] == [10041.0, 10062.0, 13640.0]
    assert state['counters']['admitted'] == 4 and state['counters']['denied'] == 1


def test_failed_start_is_settled_after_proven_dead_recovery_without_reset(tmp_path, fake_ssh, monkeypatch):
    clock, guard = Clock(), tmp_path / 'guard'
    sessions.record_external_attempt(guard, 10000.0, clock=clock)
    clock.value = 10001.0
    sessions.record_external_attempt(guard, 10001.0, clock=clock)
    clock.value = 10002.0
    original_popen, original_check = fake_ssh.popen, sessions._check
    def delayed_failed_launch(command, **kwargs):
        master = original_popen(command, **kwargs)
        clock.value += 20
        return master
    fake_ssh.failed_start = True
    monkeypatch.setattr(sessions.subprocess, 'Popen', delayed_failed_launch)
    with pytest.raises(sessions.SSHSessionError, match='connect_timeout'):
        sessions.SSHSession('karolina', guard, clock=clock)
    before = json.loads((guard / 'attempts.json').read_text())
    assert before['attempts'] == [10000.0, 10001.0, 10002.0]
    assert before['active'] is not None
    clock.value = 13602.0
    monkeypatch.setattr(sessions, '_alive', lambda pid: False)
    def prove_dead(*arguments):
        clock.value += 5
        return False
    monkeypatch.setattr(sessions, '_check', prove_dead)
    sessions.recover(guard, clock=clock)
    recovered = json.loads((guard / 'attempts.json').read_text())
    assert recovered['attempts'] == [10000.0, 10001.0, 13607.0]
    assert recovered['counters'] == before['counters']
    assert recovered['active'] is None and recovered['circuit'] is None
    fake_ssh.failed_start = False
    monkeypatch.setattr(sessions.subprocess, 'Popen', original_popen)
    monkeypatch.setattr(sessions, '_check', original_check)
    for now in (13607.0, 13608.0):
        clock.value = now
        with sessions.SSHSession('karolina', guard, clock=clock):
            pass
    clock.value = 13609.0
    with pytest.raises(sessions.SSHSessionError, match='attempt_budget_exhausted'):
        sessions.SSHSession('karolina', guard, clock=clock)
    state = json.loads((guard / 'attempts.json').read_text())
    assert state['attempts'] == [13607.0, 13607.0, 13608.0]
    assert state['counters']['admitted'] == 5 and state['counters']['denied'] == 1
    assert len(fake_ssh.masters) == 3


def test_interrupted_pre_ready_cleanup_settles_after_owned_master_reap(tmp_path, fake_ssh, monkeypatch):
    clock, guard = Clock(), tmp_path / 'guard'
    original_terminate = Master.terminate
    def interrupt_before_ready(*arguments):
        clock.value += 12
        raise KeyboardInterrupt
    def delayed_reap(master):
        clock.value += 8
        original_terminate(master)
    monkeypatch.setattr(sessions, '_check', interrupt_before_ready)
    monkeypatch.setattr(Master, 'terminate', delayed_reap)
    with pytest.raises(KeyboardInterrupt):
        sessions.SSHSession('karolina', guard, clock=clock)
    state = json.loads((guard / 'attempts.json').read_text())
    assert state['attempts'] == [10020.0]
    assert state['active'] is None and state['circuit'] is None
    assert state['counters']['admitted'] == 1
    assert fake_ssh.masters[0].poll() is not None


def test_interrupted_unknown_spawn_retains_blocking_tombstone(tmp_path, fake_ssh, monkeypatch):
    clock, guard = Clock(), tmp_path / 'guard'
    def interrupted_spawn(*arguments, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(sessions.subprocess, 'Popen', interrupted_spawn)
    with pytest.raises(KeyboardInterrupt):
        sessions.SSHSession('karolina', guard, clock=clock)
    state = json.loads((guard / 'attempts.json').read_text())
    assert state['attempts'] == [10000.0] and state['active']['master_pid'] is None
    assert state['circuit'] == 'client_error'
    with pytest.raises(sessions.SSHSessionError, match='uncertain_master_identity'):
        sessions.recover(guard, clock=clock)
    assert fake_ssh.masters == []


def test_external_attempt_import_cannot_reorder_an_active_reservation(tmp_path, fake_ssh):
    clock, guard = Clock(), tmp_path / 'guard'
    with sessions.SSHSession('karolina', guard, clock=clock):
        before = json.loads((guard / 'attempts.json').read_text())
        with pytest.raises(sessions.SSHSessionError, match='session_still_owned'):
            sessions.record_external_attempt(guard, clock.value, clock=clock)
        assert json.loads((guard / 'attempts.json').read_text()) == before


def test_excess_control_path_is_denied_before_configuration_or_launch(tmp_path, fake_ssh):
    guard = tmp_path / ("d" * 100)
    with pytest.raises(sessions.SSHSessionError, match="control_path_too_long"):
        sessions.SSHSession("karolina", guard)
    assert fake_ssh.masters == [] and fake_ssh.config_calls == 0
    assert fake_ssh.resolutions == []

@pytest.mark.parametrize("kind", ["calibration", "benchmark"])
def test_other_borrower_circuit_blocks_next_worker_claim(tmp_path, fake_ssh, kind):
    from types import SimpleNamespace
    from aws_mesouq.connector.connector import Connector
    from aws_mesouq.connector.transport import Transport
    from aws_mesouq.benchmark_worker import BenchmarkWorker
    guard = tmp_path / "guard"
    cloud_calls, claims = [], []
    def request(method, path, payload=None):
        cloud_calls.append((method, path))
        if path == "/v1/me":
            return {"subject": "owner"}
        if path == "/v1/connector/claim":
            claims.append(path)
            pytest.fail("calibration claimed after another borrower tripped circuit")
        pytest.fail("unexpected cloud operation after shared circuit")
    def benchmark_claim(*args, **kwargs):
        claims.append("benchmark")
        pytest.fail("benchmark claimed after another borrower tripped circuit")
    with sessions.SSHSession("karolina", guard):
        transport = Transport(host="karolina", state_dir=guard)
        transport._ssh_session()
        try:
            with sessions.SSHSession("karolina", guard, borrow_only=True) as other:
                fake_ssh.rpc_codes = [255, 0]
                with pytest.raises(sessions.SSHSessionError, match="connection_lost"):
                    other.run_bytes(["true"])
            before = json.loads((guard / "attempts.json").read_text())
            config_calls = fake_ssh.config_calls
            worker_type = Connector if kind == "calibration" else BenchmarkWorker
            worker = object.__new__(worker_type)
            worker.slurm = SimpleNamespace(transport=transport)
            worker.api = SimpleNamespace(request=request)
            worker.project = "fixture"
            worker.journal = SimpleNamespace(data={"jobs": {}}, save=lambda: None)
            if kind == "calibration":
                worker.connector_id = "fixture"
                worker.site = "karolina"
                worker.durable_archive_root = False
                worker.archive_root = None
            else:
                worker.state = {"stages": {}, "campaigns": {}}
                worker.maximum_active = 16
                worker.client = SimpleNamespace(claim=benchmark_claim)
            with pytest.raises(sessions.SSHSessionError, match="connection_lost"):
                worker.sweep()
            after = json.loads((guard / "attempts.json").read_text())
            assert claims == [] and cloud_calls == []
            assert after["counters"] == before["counters"]
            assert fake_ssh.rpc_codes == [0]
            assert fake_ssh.config_calls == config_calls
            assert len(fake_ssh.masters) == 1
        finally:
            transport.close()


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("failure", ["missing_socket", "dead_process"])
def test_offline_health_latches_missing_or_dead_session(tmp_path, fake_ssh, monkeypatch, cached, failure):
    guard = tmp_path / "guard"
    with sessions.SSHSession("karolina", guard) as owner:
        borrower = sessions.SSHSession("karolina", guard, borrow_only=True) if cached else None
        if failure == "missing_socket":
            owner.store.socket.unlink()
        else:
            monkeypatch.setattr(sessions, "_alive", lambda pid: False)
        config_calls = fake_ssh.config_calls
        with pytest.raises(sessions.SSHSessionError, match="connection_lost"):
            if borrower is None:
                sessions.SSHSession("karolina", guard, borrow_only=True)
            else:
                borrower.check_healthy()
        state = json.loads((guard / "attempts.json").read_text())
        assert state["circuit"] == "connection_lost"
        assert state["counters"]["rpc_requests"] == 0
        assert fake_ssh.config_calls == config_calls
        assert len(fake_ssh.masters) == 1
        if borrower is not None:
            borrower.close()


def test_health_adapter_only_tolerates_absent_hooks():
    from types import SimpleNamespace
    from aws_mesouq.connector.transport import check_transport_health
    check_transport_health(SimpleNamespace())
    with pytest.raises(TypeError):
        check_transport_health(SimpleNamespace(check_healthy=None))
    for error in (sessions.SSHSessionError("connection_lost"),
                  AttributeError("health hook failed"), OSError("health state unavailable")):
        def fail(error=error):
            raise error
        with pytest.raises(type(error)) as caught:
            check_transport_health(SimpleNamespace(check_healthy=fail))
        assert caught.value is error


def test_local_health_check_performs_no_subprocess(monkeypatch):
    from aws_mesouq.connector.transport import Transport
    monkeypatch.setattr(sessions.subprocess, "run",
                        lambda *a, **k: pytest.fail("local health launched subprocess"))
    monkeypatch.setattr(sessions.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("local health launched master"))
    transport = Transport(local=True)
    transport.check_healthy()
    assert transport._session is None

def _archive_seam_worker(transport):
    import copy
    import hashlib
    from types import SimpleNamespace
    from aws_mesouq.benchmark import DEFAULT_CONFIGURATION, checksum, scientific_request_checksum
    from aws_mesouq.benchmark_worker import BenchmarkWorker
    calls = {"claims": 0, "scans": 0, "archives": []}
    worker = object.__new__(BenchmarkWorker)
    worker.project = "p"
    worker.maximum_active = 16
    worker.operator = {"run_root": "/private/scratch", "archive_root": "/private/archive",
                       "science_revision": "b" * 40, "adapter_sha256": "a" * 64}
    worker.slurm = SimpleNamespace(transport=transport)
    worker.journal = SimpleNamespace(data={"owner_sub": "alice"}, save=lambda: None)
    configuration = copy.deepcopy(DEFAULT_CONFIGURATION)
    configuration["idempotency_key"] = "archive-seam"
    campaign = {
        "id": "campaign", "project_id": "p", "owner_sub": "alice",
        "configuration": configuration, "configuration_hash": checksum(configuration),
        "scientific_request_hash": scientific_request_checksum(configuration),
        "approved_adapter": {key: worker.operator[key]
                             for key in ("archive_root", "science_revision", "adapter_sha256")},
        "archive": {"status": "requested"}, "artifacts": [],
    }
    originals = [{"path": f"retained/file-{index:03d}.bin", "sha256": "c" * 64,
                  "size_bytes": 1} for index in range(33)]
    blob = worker._archive_manifest(campaign, originals)
    manifest = {"path": "archive-manifest.json", "sha256": hashlib.sha256(blob).hexdigest(),
                "size_bytes": len(blob)}
    worker.state = {"stages": {}, "campaigns": {campaign["id"]: {
        "root": "/private/scratch/campaign", "archive_files": [*originals, manifest],
        "archive_manifest": blob.decode(),
    }}}
    def forbidden(*args, **kwargs):
        pytest.fail("Archive failure allowed source, payload, or ACK work")
    def claim(project, request_id, **kwargs):
        assert project == "p"
        calls["claims"] += 1
        return {}
    def request(method, path, payload=None):
        assert (method, path, payload) == ("GET", "/v1/projects/p/benchmarks", None)
        calls["scans"] += 1
        return {"campaigns": [campaign, {**campaign, "id": "later-campaign"}]}
    worker.client = SimpleNamespace(claim=claim, archive=forbidden)
    worker.api = SimpleNamespace(request=request)
    worker._archive_snapshot = forbidden
    worker._immutable = forbidden
    transport.archive_prepare = forbidden
    transport.archive_copy_batch = forbidden
    archive = worker.archive
    def tracked_archive(current):
        calls["archives"].append(current["id"])
        return archive(current)
    worker.archive = tracked_archive
    return worker, calls


def test_archive_batches_preserve_json_stdin_through_shared_session(tmp_path, fake_ssh, monkeypatch):
    import shlex
    from aws_mesouq.connector.archive import ARCHIVE_HELPER
    from aws_mesouq.connector.transport import Transport
    guard = tmp_path / "guard"
    with sessions.SSHSession("karolina", guard) as owner:
        transport = Transport(host="karolina", state_dir=guard)
        original_run = sessions.subprocess.run
        captured = []
        def capture(command, **kwargs):
            if "-G" not in command and "-O" not in command:
                captured.append((list(command), kwargs.get("input")))
            return original_run(command, **kwargs)
        monkeypatch.setattr(sessions.subprocess, "run", capture)
        proof = {
            "root": {"dev": 1, "ino": 2, "uid": os.getuid(), "mode": 0o700},
            "file": {"dev": 1, "ino": 3, "uid": os.getuid(), "mode": 0o600,
                     "nlink": 2, "size": 7, "mtime_ns": 4, "ctime_ns": 5},
            "paths": ["a.bin", "b.bin"],
        }
        items = [{"source_root": "/private/scratch/campaign", "source": name,
                  "destination": name, "sha256": "a" * 64, "size_bytes": 7,
                  "source_link_group": proof} for name in proof["paths"]]
        verification = [{key: item[key] for key in ("destination", "sha256", "size_bytes")}
                        for item in items]
        before = owner.snapshot()["counters"]["rpc_requests"]
        try:
            transport.archive_copy_batch("/private/archive", "campaign", items)
            fake_ssh.rpc_codes = [3, 0]
            with pytest.raises(subprocess.CalledProcessError) as failure:
                transport.archive_verify_batch("/private/archive", "campaign", verification)
            assert failure.value.returncode == 3
            assert owner.snapshot()["circuit"] is None
            transport.archive_verify_batch("/private/archive", "campaign", verification)
            expected = [("copy-batch", items), ("verify-batch", verification),
                        ("verify-batch", verification)]
            assert len(captured) == len(expected)
            for (command, payload), (operation, values) in zip(captured, expected):
                assert shlex.split(command[-1]) == [
                    transport.staging_python, "-I", "-c", ARCHIVE_HELPER,
                    "/private/archive", "campaign", operation,
                ]
                assert payload == json.dumps(values, allow_nan=False, separators=(",", ":")).encode()
                assert len(payload) <= 4 * 1024 * 1024
            snapshot = owner.snapshot()
            assert snapshot["circuit"] is None
            assert snapshot["counters"]["rpc_requests"] - before == 3
            assert snapshot["counters"]["admitted"] == 1
            assert len(fake_ssh.masters) == 1
        finally:
            transport.close()
        assert fake_ssh.masters[0].poll() is None


def test_cached_missing_then_corrupt_archive_stops_without_tripping_ssh(tmp_path, fake_ssh):
    from aws_mesouq.benchmark_worker import ArtifactIntegrityError
    from aws_mesouq.connector.transport import Transport
    guard = tmp_path / "guard"
    with sessions.SSHSession("karolina", guard) as owner:
        transport = Transport(host="karolina", state_dir=guard)
        worker, calls = _archive_seam_worker(transport)
        fake_ssh.rpc_codes = [3, 1, 0]
        before = owner.snapshot()["counters"]["rpc_requests"]
        try:
            with pytest.raises(ArtifactIntegrityError) as failure:
                worker.sweep()
            assert isinstance(failure.value.__cause__, subprocess.CalledProcessError)
            assert failure.value.__cause__.returncode == 1
            assert calls == {"claims": 1, "scans": 1, "archives": ["campaign"]}
            assert fake_ssh.rpc_codes == [0]
            snapshot = owner.snapshot()
            assert snapshot["circuit"] is None
            assert snapshot["counters"]["rpc_requests"] - before == 2
            assert snapshot["counters"]["admitted"] == 1
            assert len(fake_ssh.masters) == 1
            assert not worker.state["campaigns"]["campaign"].get("archive_verified")
        finally:
            transport.close()


@pytest.mark.parametrize("failure,category", [("255", "connection_lost"),
                                             ("timeout", "command_timeout")])
def test_archive_transport_failure_blocks_later_claim_and_sweep(tmp_path, fake_ssh, monkeypatch, failure, category):
    from aws_mesouq.connector.transport import Transport
    guard = tmp_path / "guard"
    with sessions.SSHSession("karolina", guard) as owner:
        transport = Transport(host="karolina", state_dir=guard)
        worker, calls = _archive_seam_worker(transport)
        original_run = sessions.subprocess.run
        rpc_calls = []
        fake_ssh.rpc_codes = [255, 0] if failure == "255" else [0]
        def fail_rpc(command, **kwargs):
            if "-G" not in command and "-O" not in command:
                rpc_calls.append((list(command), kwargs.get("input")))
                if failure == "timeout":
                    raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            return original_run(command, **kwargs)
        monkeypatch.setattr(sessions.subprocess, "run", fail_rpc)
        before = owner.snapshot()["counters"]["rpc_requests"]
        try:
            with pytest.raises(sessions.SSHSessionError) as caught:
                worker.sweep()
            assert caught.value.category == category
            assert calls == {"claims": 1, "scans": 1, "archives": ["campaign"]}
            previous_calls = (calls["claims"], calls["scans"], tuple(calls["archives"]))
            for operation, args in [(worker._claim, ()), (worker.sweep, ()),
                                    (worker._refresh, ({},)), (worker.poll, ({},))]:
                with pytest.raises(sessions.SSHSessionError) as caught:
                    operation(*args)
                assert caught.value.category == category
            assert (calls["claims"], calls["scans"], tuple(calls["archives"])) == previous_calls
            assert len(rpc_calls) == 1
            assert fake_ssh.rpc_codes == [0]
            snapshot = owner.snapshot()
            assert snapshot["circuit"] == category
            assert snapshot["counters"]["rpc_requests"] - before == 1
            assert snapshot["counters"]["admitted"] == 1
            assert len(fake_ssh.masters) == 1
            assert not worker.state["campaigns"]["campaign"].get("archive_verified")
        finally:
            transport.close()
