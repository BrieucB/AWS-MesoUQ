"""Shared SSH failure must halt the whole coordinator before more work is claimed."""
from types import SimpleNamespace
import json
import pytest
from aws_mesouq.benchmark_worker import BenchmarkWorker, ArtifactIntegrityError
from aws_mesouq.connector.ssh_session import SSHSessionError

def skeleton():
    worker = object.__new__(BenchmarkWorker)
    worker.state = {"stages": {}, "campaigns": {}}
    worker.maximum_active = 16
    worker.project = "fixture"
    return worker

def test_first_stage_failure_prevents_other_stages_claims_and_archives():
    worker = skeleton()
    first, second = {"cloud_terminal": False}, {"cloud_terminal": False}
    worker.state["stages"] = {"a": first, "b": second}
    seen = []
    def poll(entry):
        seen.append(entry)
        raise SSHSessionError("connection_lost")
    worker.poll = poll
    worker._claim = lambda: pytest.fail("must not claim after shared transport failure")
    worker.api = SimpleNamespace(request=lambda *a: pytest.fail("must not scan archives"))
    with pytest.raises(SSHSessionError, match="connection_lost"):
        worker.sweep()
    assert seen == [first]

def test_new_stage_failure_prevents_another_claim():
    worker, claims = skeleton(), []
    def claim():
        claims.append(1)
        if len(claims) > 1:
            pytest.fail("must not claim another stage")
        entry = {"cloud_terminal": False}
        worker.state["stages"]["a"] = entry
        return entry
    worker._claim = claim
    def poll(entry):
        raise SSHSessionError("connection_lost")
    worker.poll = poll
    worker.api = SimpleNamespace(request=lambda *a: pytest.fail("must not scan archives"))
    with pytest.raises(SSHSessionError, match="connection_lost"):
        worker.sweep()
    assert len(claims) == 1

@pytest.mark.parametrize("error", [SSHSessionError("connection_lost"), ArtifactIntegrityError("fixture integrity mismatch")])
def test_archive_failure_stops_the_archive_loop(error):
    worker = skeleton()
    worker._claim = lambda: None
    campaigns = [{"id": "a", "archive": {"status": "requested"}}, {"id": "b", "archive": {"status": "requested"}}]
    worker.api = SimpleNamespace(request=lambda *a: {"campaigns": campaigns})
    seen = []
    def archive(campaign):
        seen.append(campaign["id"])
        raise error
    worker.archive = archive
    with pytest.raises(type(error)):
        worker.sweep()
    assert seen == ["a"]

@pytest.mark.parametrize("kind", ["benchmark", "calibration"])
def test_entrypoint_guard_failure_is_sanitized_and_closes_without_browser_login(tmp_path, monkeypatch, capsys, kind):
    from aws_mesouq.connector import transport as transport_module
    from aws_mesouq.connector import auth, slurm, api, journal
    import aws_mesouq.benchmark_worker as benchmark
    from aws_mesouq.connector import cli
    closed, journals = [], []
    class Blocked:
        def __init__(self, **kwargs):
            pass
        def identity(self):
            raise SSHSessionError("connection_lost")
        def close(self):
            closed.append(True)
    class SyntheticJournal:
        def __init__(self, path):
            journals.append("opened")
        def close(self):
            journals.append("closed")
    monkeypatch.setattr(transport_module, "Transport", Blocked)
    monkeypatch.setattr(auth, "browser_login", lambda *a: pytest.fail("must not authenticate without transport"))
    monkeypatch.setattr(api, "API", lambda *a: pytest.fail("must not call API"))
    monkeypatch.setattr(slurm, "Slurm", lambda transport: SimpleNamespace())
    directory = tmp_path / "synthetic-journal"
    directory.mkdir(mode=0o700)
    (directory / "journal.json").write_text("{}")
    shared = ["--api-url", "https://api.invalid", "--project", "fixture", "--oauth-domain", "https://auth.invalid",
              "--client-id", "fixture", "--ssh-host", "fixture", "--journal", str(directory), "--once"]
    if kind == "benchmark":
        operator = tmp_path / "operator.json"
        operator.write_text(json.dumps({"expected_user": "fixture", "expected_host": "fixture.cluster"}))
        monkeypatch.setattr(benchmark, "validate_operator", lambda value: value)
        monkeypatch.setattr(benchmark, "existing_identity", lambda selected: None)
        monkeypatch.setattr(journal, "Journal", SyntheticJournal)
        result = benchmark.main(shared + ["--operator-config", str(operator)])
        assert journals == ["opened", "closed"]
    else:
        monkeypatch.setattr(cli, "Transport", Blocked)
        monkeypatch.setattr(cli, "browser_login", lambda *a: pytest.fail("must not authenticate"))
        monkeypatch.setattr(cli, "Journal", lambda *a: pytest.fail("must not open journal before preflight"))
        result = cli.main(shared + ["--site", "karolina", "--expected-user", "fixture", "--expected-host", "fixture.cluster",
                                   "--run-root", str(tmp_path / "run"), "--repository", str(tmp_path / "repository"),
                                   "--model-registry", str(tmp_path / "models.json")])
    assert result == 2 and closed == [True]
    body = json.loads(capsys.readouterr().out)
    assert body.pop("observed_at") > 0
    assert body == {"connector_blocked": True, "error_category": "connection_lost", "recovery_required": True}
