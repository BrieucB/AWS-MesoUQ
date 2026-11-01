"""Maintained Benchmark Lab authentication controller and managed worker.

Legacy flags still delegate to benchmark_worker. Managed workers never log in.
The controller accepts lifecycle commands only from its owner's standard input.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import select
import signal
import stat
import sys
import threading
import time

from aws_mesouq.connector.auth import SessionExpired, browser_login
from aws_mesouq.connector.auth_broker import AuthBroker, BrokerError
from aws_mesouq.connector.broker_client import BrokerClient
from aws_mesouq.connector.benchmark_supervisor import SourceWorkerSpec, WorkerSupervisor

MAX_COMMAND_BYTES = 8192
MAX_MANIFEST_BYTES = 1024 * 1024
WORKER_ENTRYPOINTS = {"benchmark-v1": "aws_mesouq/benchmark_cli.py",
                      "benchmark-all": "aws_mesouq/benchmark_dispatch.py"}
# Adding a worker kind requires reviewed source registration, never an RPC import path.


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise BrokerError("invalid_owner_command")
        value[key] = item
    return value


def _json(data):
    try:
        return json.loads(data, object_pairs_hook=_unique,
                          parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, UnicodeError):
        raise BrokerError("invalid_owner_command") from None


def _manifest(path):
    """Read public release metadata without following any ancestor symlinks."""
    path = Path(path)
    if not path.is_absolute() or str(path) != os.path.abspath(path):
        raise BrokerError("invalid_source_manifest")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            mode = stat.S_IMODE(info.st_mode)
            if info.st_uid not in (0, os.geteuid()) or (
                mode & 0o022 and not (info.st_uid == 0 and mode & stat.S_ISVTX)
            ):
                raise BrokerError("invalid_source_manifest")
        leaf = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            before = os.fstat(leaf)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or before.st_uid not in (0, os.geteuid())
                    or stat.S_IMODE(before.st_mode) & 0o022
                    or not 0 < before.st_size <= MAX_MANIFEST_BYTES):
                raise BrokerError("invalid_source_manifest")
            data = bytearray()
            while len(data) <= MAX_MANIFEST_BYTES:
                chunk = os.read(leaf, min(65536, MAX_MANIFEST_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            after = os.fstat(leaf)
            fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_nlink",
                      "st_size", "st_mtime_ns", "st_ctime_ns")
            if len(data) > MAX_MANIFEST_BYTES or any(
                getattr(before, key) != getattr(after, key) for key in fields
            ):
                raise BrokerError("invalid_source_manifest")
        finally:
            os.close(leaf)
    except OSError:
        raise BrokerError("invalid_source_manifest") from None
    finally:
        os.close(fd)
    return _json(bytes(data))


def _runtime_arguments(args, *, kind=None):
    selected = kind or getattr(args, "worker_kind", "benchmark-v1")
    if selected not in WORKER_ENTRYPOINTS:
        raise BrokerError("unknown_worker_kind")
    values = ["managed-worker", "--api-url", args.api_url, "--project", args.project,
              "--operator-config", str(args.operator_config), "--journal", str(args.journal),
              "--poll-seconds", str(args.poll_seconds)]
    if selected == "benchmark-all":
        registry = getattr(args, "accuracy_contexts", None)
        if registry is None:
            raise BrokerError("accuracy_contexts_required")
        values += ["--worker-kind", selected, "--accuracy-contexts", str(registry)]
    if args.ssh_host:
        values += ["--ssh-host", args.ssh_host, "--ssh-state-dir", str(args.ssh_state_dir)]
    else:
        values += ["--local-cluster"]
    if args.once:
        values += ["--once"]
    return tuple(values)


def approved_worker(package_root, manifest_path, binding, arguments, *, kind="benchmark-v1"):
    if kind not in WORKER_ENTRYPOINTS:
        raise BrokerError("unknown_worker_kind")
    manifest = _manifest(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("entrypoint") != WORKER_ENTRYPOINTS[kind]:
        raise BrokerError("unregistered_worker_entrypoint")
    specification = SourceWorkerSpec.from_manifest(
        package_root, manifest, binding, arguments=tuple(arguments)
    )
    specification.validate()
    return specification


class Controller:
    """Owner-controlled process replacement; auth and the shared master outlive workers."""

    def __init__(self, supervisor, *, specification_loader=None):
        self.supervisor = supervisor
        self.specification_loader = specification_loader
        self.last_error = None

    def start(self):
        # Authentication must succeed once, even when a worker cannot start.
        self.supervisor._spec.validate()
        self.supervisor.broker.start()
        try:
            self.supervisor.start_worker()
        except Exception:
            self.last_error = "worker_start_failed"
        return self.status()

    def status(self):
        value = self.supervisor.safe_status()
        active = value["worker_pid"] is not None and value["worker_exit_code"] is None
        return {**value, "worker_state": "running" if active else "paused",
                "controller_error": self.last_error or (None if active else "worker_exited")}

    def command(self, value):
        if not isinstance(value, dict) or not isinstance(value.get("operation"), str):
            raise BrokerError("invalid_owner_command")
        operation = value["operation"]
        required = {"operation"} if operation != "use_spec" else {
            "operation", "package_root", "manifest", "binding", "worker_kind"}
        if set(value) != required:
            raise BrokerError("invalid_owner_command")
        if operation == "status":
            return self.status()
        if operation == "stop":
            return {"stopping": True}
        if operation not in ("restart", "use_spec"):
            raise BrokerError("unknown_owner_operation")
        try:
            if operation == "use_spec":
                if self.specification_loader is None or any(
                    not isinstance(value[key], str) or len(value[key].encode()) > 4096
                    for key in required - {"operation"}
                ):
                    raise BrokerError("invalid_source_specification")
                specification = self.specification_loader(value)
                self.supervisor.apply_spec(specification)
            else:
                self.supervisor.restart_worker()
            self.last_error = None
        except Exception:
            # Keep the controller/token session alive. Never echo paths/provider errors.
            self.last_error = "worker_restart_failed"
        return self.status()

    def close(self):
        self.supervisor.close()


def _runtime_parser(parser):
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--operator-config", required=True, type=Path)
    parser.add_argument("--journal", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--ssh-host")
    mode.add_argument("--local-cluster", action="store_true")
    parser.add_argument("--ssh-state-dir", type=Path)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=20)
    parser.add_argument("--worker-kind", choices=tuple(WORKER_ENTRYPOINTS), default="benchmark-v1")
    parser.add_argument("--accuracy-contexts", type=Path)


def _validate_runtime(parser, args):
    if args.worker_kind == "benchmark-all":
        if args.accuracy_contexts is None or not args.accuracy_contexts.is_absolute():
            parser.error("Mixed benchmark dispatch requires an absolute reviewed accuracy-context registry")
    elif args.accuracy_contexts is not None:
        parser.error("Accuracy contexts require the registered mixed benchmark worker")
    if not math.isfinite(args.poll_seconds) or args.poll_seconds < 1:
        parser.error("Poll interval must be finite and at least one second")
    if args.ssh_host and args.ssh_state_dir is None:
        parser.error("An existing guarded SSH state directory is required")
    if not args.journal.is_dir() or not (args.journal / "journal.json").is_file():
        parser.error("An existing enrolled normal connector journal is required")


def run_managed_worker(args, *, api_factory=None, transport_factory=None, stop=None):
    """Use normal v1 journal/identity/claims; injectable external boundaries aid synthetic QA.

    CLI exposes no dependency/import overrides. There is deliberately no login fallback.
    """
    from aws_mesouq.benchmark_worker import BenchmarkWorker, existing_identity, validate_operator
    from aws_mesouq.connector.api import API
    from aws_mesouq.connector.journal import Journal
    from aws_mesouq.connector.slurm import Slurm
    from aws_mesouq.connector.transport import Transport

    tokens = BrokerClient.from_environment()
    operator = validate_operator(json.loads(args.operator_config.read_text()))
    transport = (transport_factory or Transport)(
        host=args.ssh_host, local=args.local_cluster, state_dir=args.ssh_state_dir)
    journal = None
    stop = stop if stop is not None else threading.Event()
    try:
        journal = Journal(args.journal)
        existing_identity(journal)
        slurm = Slurm(transport)
        if (transport.identity() != operator["expected_user"]
                or transport.run(["hostname", "-f"]).strip() != operator["expected_host"]):
            raise BrokerError("worker_identity_mismatch")
        api = (api_factory or API)(args.api_url, tokens)
        policy = ({"propagate_auth_errors": True}
                  if getattr(args, "worker_kind", "benchmark-v1") == "benchmark-all" else {})
        worker = BenchmarkWorker(api, slurm, journal, project=args.project, operator=operator, **policy)
        if getattr(args, "worker_kind", "benchmark-v1") == "benchmark-all":
            from aws_mesouq.benchmark_dispatch import FairBenchmarkWorker, build_accuracy_worker
            accuracy = build_accuracy_worker(api, slurm, journal, project=args.project,
                                             registry_path=args.accuracy_contexts)
            worker = FairBenchmarkWorker(worker, accuracy, journal, project=args.project,
                                         owner_sub=journal.data["owner_sub"], stop=stop)
        while not stop.is_set():
            worker.sweep()
            if args.once:
                break
            stop.wait(args.poll_seconds)
        return 0
    except SessionExpired:
        return 3
    except Exception:
        # The supervisor reports the code; never print arbitrary exception text or payloads.
        return 2
    finally:
        try:
            if journal is not None:
                journal.close()
        finally:
            transport.close()


def _emit(stream, value):
    stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    stream.flush()


def run_controller_loop(controller, *, input_fd, output, stop, startup_complete=None):
    """Bounded owner stdin. EOF detaches control; it does not drop auth or kill jobs."""
    buffer = bytearray()
    detached = False
    discard = False
    if stop.is_set():
        return 0
    _emit(output, controller.start())
    if startup_complete is not None:
        startup_complete.set()
    while not stop.is_set():
        if detached:
            stop.wait(0.2)
            continue
        if not select.select([input_fd], [], [], 0.2)[0]:
            continue
        chunk = os.read(input_fd, 4096)
        if not chunk:
            detached = True
            buffer.clear()
            continue
        for item in chunk:
            if item == 10:
                if discard:
                    discard = False
                    buffer.clear()
                    continue
                line = bytes(buffer)
                buffer.clear()
                try:
                    response = controller.command(_json(line))
                except Exception:
                    response = {"controller_command_error": "invalid_owner_command"}
                _emit(output, response)
                if response.get("stopping") is True:
                    stop.set()
                    break
            elif not discard:
                buffer.append(item)
                if len(buffer) > MAX_COMMAND_BYTES:
                    buffer.clear()
                    discard = True
                    _emit(output, {"controller_command_error": "owner_command_too_large"})
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("controller", "managed-worker"):
        from aws_mesouq.benchmark_worker import main as legacy_main
        return legacy_main(argv)
    mode = argv.pop(0)
    parser = argparse.ArgumentParser(description=__doc__)
    _runtime_parser(parser)
    if mode == "controller":
        parser.add_argument("--oauth-domain", required=True)
        parser.add_argument("--client-id", required=True)
        parser.add_argument("--callback-port", type=int, default=8765)
        parser.add_argument("--worker-package", type=Path, required=True)
        parser.add_argument("--worker-manifest", type=Path, required=True)
        parser.add_argument("--worker-binding", required=True)
        parser.add_argument("--socket-dir", type=Path)
    args = parser.parse_args(argv)
    _validate_runtime(parser, args)
    stop = threading.Event()
    startup_complete = threading.Event()
    shutdown_signal = None
    previous = {}
    controller = None
    transport_guard = None
    def request_stop(signum, _frame):
        nonlocal shutdown_signal
        shutdown_signal = signum
        stop.set()
        if mode == "controller" and not startup_complete.is_set():
            # Unwind the real callback-server context and broker listeners.
            # A cancelled authentication must never start a worker later.
            raise KeyboardInterrupt
    try:
        for selected in (signal.SIGTERM, signal.SIGINT):
            previous[selected] = signal.signal(selected, request_stop)
        if mode == "managed-worker":
            return run_managed_worker(args, stop=stop)
        arguments = _runtime_arguments(args)
        def load(value):
            return approved_worker(value["package_root"], value["manifest"],
                                   value["binding"], _runtime_arguments(args, kind=value["worker_kind"]),
                                   kind=value["worker_kind"])
        specification = approved_worker(args.worker_package, args.worker_manifest,
                                        args.worker_binding, arguments, kind=args.worker_kind)
        # Borrow only: cold starts require an independently maintained normal guarded master.
        if args.ssh_host:
            from aws_mesouq.connector.ssh_session import SSHSession
            transport_guard = SSHSession(args.ssh_host, args.ssh_state_dir, borrow_only=True)
            transport_guard.check_healthy()
        broker = AuthBroker(
            lambda: browser_login(args.oauth_domain, args.client_id, args.callback_port),
            private_socket_dir=args.socket_dir)
        controller = Controller(WorkerSupervisor(broker, specification),
                                specification_loader=load)
        return run_controller_loop(controller, input_fd=sys.stdin.fileno(),
                                   output=sys.stdout, stop=stop, startup_complete=startup_complete)
    except KeyboardInterrupt:
        return 128 + (shutdown_signal or signal.SIGINT)
    except SystemExit:
        raise
    except Exception:
        _emit(sys.stdout, {"controller_blocked": True, "error_code": "controller_start_failed"})
        return 2
    finally:
        try:
            if controller is not None:
                controller.close()
        finally:
            if transport_guard is not None:
                transport_guard.close()
            for selected, handler in previous.items():
                signal.signal(selected, handler)


if __name__ == "__main__":
    raise SystemExit(main())
