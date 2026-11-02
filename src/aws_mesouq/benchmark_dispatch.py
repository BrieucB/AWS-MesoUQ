"""One journal owner and authentication session for both benchmark protocols."""
from __future__ import annotations
import threading

PROTOCOLS = ("benchmark-v1", "benchmark-accuracy-v2")
SCHEMA = "aws_mesouq.fair_benchmark_dispatch.v1"


def build_accuracy_worker(api, slurm, journal, *, project, registry_path):
    from aws_mesouq.benchmark_accuracy_client import AccuracyClient
    from aws_mesouq.benchmark_accuracy_contexts import AccuracyExecutionContexts
    from aws_mesouq.benchmark_accuracy_worker import AccuracyWorker
    from aws_mesouq.adapters.benchmark_accuracy import AccuracyNativeExecutor
    owner = journal.data["owner_sub"]
    contexts = AccuracyExecutionContexts.from_file(registry_path, project=project, owner_sub=owner)
    return AccuracyWorker(AccuracyClient(api, journal, owner), slurm, journal, project=project,
                          contexts=contexts, executor=AccuracyNativeExecutor(slurm, contexts))


class FairBenchmarkWorker:
    """Synchronous round robin; replacement waits for completed calls.

    An active count of zero is never taken as proof of an empty cloud queue.
    Worker replacement never implicitly cancels scheduler allocations.
    """
    def __init__(self, legacy, accuracy, journal, *, project, owner_sub, stop=None):
        self.workers = dict(zip(PROTOCOLS, (legacy, accuracy)))
        self.journal = journal
        self.stop = stop if stop is not None else threading.Event()
        value = journal.data.get("benchmark_dispatch")
        if value is None:
            value = {"schema": SCHEMA, "project_id": project, "owner_sub": owner_sub,
                     "next_protocol": PROTOCOLS[0], "last_errors": {name: None for name in PROTOCOLS}}
            journal.data["benchmark_dispatch"] = value
        if (not isinstance(value, dict)
                or set(value) != {"schema", "project_id", "owner_sub", "next_protocol", "last_errors"}
                or value["schema"] != SCHEMA or value["project_id"] != project
                or value["owner_sub"] != owner_sub or value["next_protocol"] not in PROTOCOLS
                or not isinstance(value["last_errors"], dict)
                or set(value["last_errors"]) != set(PROTOCOLS)
                or any(item is not None and not isinstance(item, str)
                       for item in value["last_errors"].values())):
            raise ValueError("Benchmark dispatch namespace differs from its owner or schema")
        self.state = value
        self.journal.save()

    @staticmethod
    def _fatal(error):
        from aws_mesouq.connector.auth import SessionExpired
        from aws_mesouq.connector.api import APIError
        from aws_mesouq.connector.ssh_session import SSHSessionError
        return (isinstance(error, (SessionExpired, SSHSessionError))
                or isinstance(error, APIError) and error.status in (401, 403))

    def sweep(self):
        result = {"attempted": 0, "failures": {}}
        for _ in PROTOCOLS:
            if self.stop.is_set():
                break
            name = self.state["next_protocol"]
            self.state["next_protocol"] = PROTOCOLS[(PROTOCOLS.index(name) + 1) % len(PROTOCOLS)]
            self.journal.save()
            result["attempted"] += 1
            try:
                self.workers[name].sweep()
            except Exception as error:
                code = type(error).__name__
                self.state["last_errors"][name] = code
                result["failures"][name] = code
                self.journal.save()
                if self._fatal(error):
                    raise
            else:
                self.state["last_errors"][name] = None
                self.journal.save()
        return result


if __name__ == "__main__":
    from aws_mesouq.benchmark_cli import main
    raise SystemExit(main())
