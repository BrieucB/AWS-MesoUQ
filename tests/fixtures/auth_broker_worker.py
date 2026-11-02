"""Pinned test entrypoint. It uses synthetic HTTP, never a real AWS/OAuth endpoint."""
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import signal
import socket
import struct
import threading
import time
import urllib.error
import uuid

from aws_mesouq.benchmark_client import BenchmarkClient
from aws_mesouq.connector.api import API
import aws_mesouq.connector.api as api_module
from aws_mesouq.connector.auth import SessionExpired
from aws_mesouq.connector.auth_broker import (
    BrokerError, MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES,
    _read_message, _send_message,
)
from aws_mesouq.connector.broker_client import (
    BrokerClient, CONTROLLER_ENV, GENERATION_ENV, SOCKET_ENV,
)
from aws_mesouq.connector.journal import Journal


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--journal", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--mode", default="stay")
    parser.add_argument("--control")
    args = parser.parse_args()
    result_path = Path(args.result)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    def save(value: dict) -> None:
        temp = result_path.with_suffix(".temporary")
        temp.write_text(json.dumps(value, sort_keys=True) + "\n")
        temp.chmod(0o600)
        os.replace(temp, result_path)

    def raw_rpc(value: dict | None = None, wire: bytes | None = None) -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(6)
            conn.connect(os.environ[SOCKET_ENV])
            if wire is None:
                _send_message(conn, value, MAX_REQUEST_BYTES)
            else:
                conn.sendall(wire)
            return _read_message(conn, MAX_RESPONSE_BYTES)

    provider = BrokerClient.from_environment()
    generation = int(os.environ[GENERATION_ENV])
    if args.mode == "guards":
        base = {"version": 1, "generation": generation}
        messages = [
            {**base, "operation": "login"},
            {**base, "operation": "restart"},
            {**base, "operation": "status", "extra": "not accepted"},
            {**base, "operation": "status", "generation": generation + 1},
            {**base, "operation": "token", "force_refresh": 1, "observed_epoch": 0},
            {**base, "operation": "token", "force_refresh": False, "observed_epoch": True},
            {**base, "operation": "token", "force_refresh": False, "observed_epoch": 0, "refresh_token": "not accepted"},
            {**base, "operation": "status", "version": True},
        ]
        replies = [raw_rpc(value) for value in messages]
        duplicate = ('{"version":1,"version":1,"operation":"status","generation":' + str(generation) + "}").encode()
        replies.append(raw_rpc(wire=struct.pack("!I", len(duplicate)) + duplicate))
        replies.append(raw_rpc(wire=struct.pack("!I", MAX_REQUEST_BYTES + 1)))
        nan = ('{"version":1,"operation":"token","generation":' + str(generation) + ',"force_refresh":false,"observed_epoch":NaN}').encode()
        replies.append(raw_rpc(wire=struct.pack("!I", len(nan)) + nan))
        save({
            "phase": "ready", "pid": os.getpid(), "generation": generation,
            "guard_errors": [value.get("error") for value in replies],
            "all_denied_without_tokens": all(
                value.get("ok") is False and "token" not in value
                for value in replies
            ),
        })
        stop.wait(30)
        return

    journal = Journal(Path(args.journal))
    api_attempts = 0
    try:
        class FakeOpener:
            def open(self, request, timeout=None):
                nonlocal api_attempts
                api_attempts += 1
                # Exercise the actual API's bearer-header construction with the
                # actual broker provider. No raw token enters a result or log.
                if request.get_header("Authorization") != "Bearer " + provider.token():
                    raise RuntimeError("synthetic_api_bearer_mismatch")
                if args.mode == "api401" and api_attempts == 1:
                    raise urllib.error.HTTPError(
                        request.full_url, 401, "synthetic unauthorized", {},
                        io.BytesIO(b'{"error":{"code":"unauthorized"}}'),
                    )
                return io.BytesIO(b'{"campaign":{"id":"qa-campaign"}}')

        api_module.urllib.request.build_opener = lambda *_: FakeOpener()
        client = BenchmarkClient(
            API("https://api.example.test", provider), journal, "qa-owner"
        )
        claim = client.build_claim_body("qa-project", uuid.uuid4().hex)
        reply = client.get("qa-project", "qa-campaign")
        if reply != {"campaign": {"id": "qa-campaign"}}:
            raise RuntimeError("synthetic_client_response_mismatch")
        token = provider.token()
        result = {
            "phase": "ready", "pid": os.getpid(), "generation": generation,
            "connector_id": claim["connector_id"],
            "normal_BenchmarkClient_used": True,
            "normal_API_used": True,
            "api_attempts": api_attempts,
            "access_token_absent_from_environment": all(
                token not in value for value in os.environ.values()
            ),
            "access_token_absent_from_argv": all(token not in value for value in os.sys.argv),
            "refresh_secret_not_available_on_provider": not hasattr(provider, "refresh_token"),
            "controller_pid": int(os.environ[CONTROLLER_ENV]),
            "status": provider.safe_status(),
        }
        if args.mode == "concurrent":
            barrier = threading.Barrier(8)
            failures = []
            successes = []
            lock = threading.Lock()

            def request_token() -> None:
                try:
                    other = BrokerClient.from_environment()
                    other.token()
                    barrier.wait(timeout=5)
                    value = other.token(force_refresh=True)
                    with lock:
                        successes.append(bool(value))
                except Exception as error:
                    with lock:
                        failures.append(type(error).__name__)

            threads = [threading.Thread(target=request_token) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
            result["concurrent_successes"] = len(successes)
            result["concurrent_failures"] = failures
            result["status"] = provider.safe_status()
        save(result)
        if args.mode == "wait-force":
            control = Path(args.control)
            deadline = time.monotonic() + 15
            while not control.exists() and time.monotonic() < deadline and not stop.is_set():
                time.sleep(0.02)
            try:
                provider.token(force_refresh=True)
                save({**result, "phase": "forced", "new_token_denied": False})
            except BrokerError as error:
                save({
                    **result, "phase": "forced", "new_token_denied": True,
                    "controlled_error": error.args[0],
                })
        if args.mode == "once":
            return
        stop.wait(30)
    except (SessionExpired, BrokerError) as error:
        save({
            "phase": "error", "pid": os.getpid(), "generation": generation,
            "error_type": type(error).__name__, "controlled_error": error.args[0],
        })
        stop.wait(30)
    finally:
        journal.close()


if __name__ == "__main__":
    main()
