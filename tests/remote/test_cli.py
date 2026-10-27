from types import SimpleNamespace

from aws_mesouq.connector import cli


def test_once_runs_one_bounded_sweep_and_closes_journal(tmp_path, monkeypatch):
    registry = tmp_path / 'registry.json'
    registry.write_text('{}')
    performed = []
    class Transport:
        def __init__(self, **kwargs):
            pass
        def identity(self):
            return 'ownuser'
        def run(self, argv):
            return 'verified.cluster\n'
    class Journal:
        def __init__(self, directory):
            pass
        def close(self):
            performed.append('closed')
    class Connector:
        def __init__(self, *args, **kwargs):
            pass
        def sweep(self):
            performed.append('sweep')
            return ['job-a', 'job-b']
        def tick(self):
            raise AssertionError('CLI must use bounded sweep')
    monkeypatch.setattr(cli, 'Transport', Transport)
    monkeypatch.setattr(cli, 'Journal', Journal)
    monkeypatch.setattr(cli, 'Connector', Connector)
    monkeypatch.setattr(cli, 'Slurm', lambda transport: SimpleNamespace())
    monkeypatch.setattr(cli, 'browser_login', lambda *args: 'in-memory')
    monkeypatch.setattr(cli, 'API', lambda *args: SimpleNamespace())
    monkeypatch.setattr(cli.time, 'sleep', lambda *_args: (_ for _ in ()).throw(AssertionError('once must not sleep')))
    result = cli.main(['--once', '--site', 'karolina', '--api-url', 'https://api', '--project', 'p',
        '--oauth-domain', 'https://auth', '--client-id', 'public-client', '--local-cluster',
        '--expected-user', 'ownuser', '--expected-host', 'verified.cluster', '--run-root', '/private/runs',
        '--repository', '/trusted/repo', '--model-registry', str(registry), '--journal', str(tmp_path / 'journal')])
    assert result == 0
    assert performed == ['sweep', 'closed']
