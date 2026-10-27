"""Public unit tests use synthetic credentials, never the operator's AWS session."""
import pytest


@pytest.fixture(autouse=True)
def synthetic_aws_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-central-1")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-real-credentials"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-real-config"))
    monkeypatch.delenv("AWS_PROFILE", raising=False)
