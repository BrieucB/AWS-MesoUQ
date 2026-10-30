"""Synthetic SSH governance checks; no Karolina handshakes are made."""
import io
import json
import multiprocessing
import os
from pathlib import Path
import socket
import subprocess
import pytest
from aws_mesouq.connector import ssh_session as sessions

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
