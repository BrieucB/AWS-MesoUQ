"""Source-pinned synthetic external boundaries; never a production CLI entrypoint."""
import argparse
import json
import os
from pathlib import Path
import signal
import sys
import threading
import urllib.error
import urllib.request

from aws_mesouq import benchmark_cli
from aws_mesouq.connector.api import API
from aws_mesouq.connector.ssh_session import SSHSessionError
from aws_mesouq.connector.transport import Transport

control = json.loads(Path(sys.argv[1]).read_text())
output = Path(control["observations"])
parser = argparse.ArgumentParser()
benchmark_cli._runtime_parser(parser)
args = parser.parse_args(sys.argv[2:])
benchmark_cli._validate_runtime(parser, args)
stop = threading.Event()
for selected in (signal.SIGTERM, signal.SIGINT):
    signal.signal(selected, lambda *_unused: stop.set())


def observe(value):
    fd = os.open(output, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (json.dumps({"pid": os.getpid(), **value}, sort_keys=True) + "\n").encode())
    finally:
        os.close(fd)


class SyntheticTransport(Transport):
    def identity(self):
        if Path(control["fail_transport"]).exists():
            raise SSHSessionError("connection_lost")
        return "fixtureuser"

    def check_healthy(self):
        if Path(control["fail_transport"]).exists():
            raise SSHSessionError("connection_lost")

    def run(self, argv, *, data=None):
        if argv == ["hostname", "-f"]:
            return "fixture.cluster\n"
        raise AssertionError("No native operations permitted in the CLI synthetic fixture")


class SyntheticAPI(API):
    def request(self, method, path, payload=None):
        # Exercise the exact broker token interface and normal public worker protocol.
        token = self.token.token()
        request = urllib.request.Request(
            control["endpoint"] + path,
            data=None if payload is None else json.dumps(payload).encode(),
            method=method,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            raise AssertionError("Synthetic control plane rejected normal worker request") from error
        observe({"method": method, "path": path, "api_ack": True})
        return result


observe({"phase": "entry"})
code = benchmark_cli.run_managed_worker(
    args, api_factory=SyntheticAPI, transport_factory=SyntheticTransport, stop=stop)
observe({"phase": "exit", "code": code})
raise SystemExit(code)
