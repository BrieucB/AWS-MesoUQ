import copy
import json
import pytest
from aws_publishing import handler
from aws_publishing.api import APIError


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("COGNITO_ISSUER", "https://identity.invalid/pool")
    monkeypatch.setenv("COGNITO_CLIENT_ID", "browser-client")

    class App:
        def handle(self, method, path, claims, body):
            assert path == "/v1/me"
            return 200, {"subject": claims["sub"]}

    monkeypatch.setattr(handler, "application", lambda: App())
    return {
        "rawPath": "/v1/me",
        "requestContext": {
            "http": {"method": "GET"},
            "authorizer": {
                "jwt": {
                    "claims": {
                        "sub": "alice",
                        "iss": "https://identity.invalid/pool",
                        "client_id": "browser-client",
                        "token_use": "access",
                    }
                }
            },
        },
    }


def test_gateway_access_token_required(configured):
    assert handler.handler(configured, None)["statusCode"] == 200
    for field, wrong in (
        ("iss", "https://attacker.invalid"),
        ("client_id", "another-client"),
        ("token_use", "id"),
    ):
        event = copy.deepcopy(configured)
        event["requestContext"]["authorizer"]["jwt"]["claims"][field] = wrong
        response = handler.handler(event, None)
        assert response["statusCode"] == 401
        assert json.loads(response["body"])["error"]["code"] == "unauthenticated"
    event = copy.deepcopy(configured)
    del event["requestContext"]["authorizer"]
    event["headers"] = {"authorization": "Bearer forged"}
    assert handler.handler(event, None)["statusCode"] == 401


def test_preflight_returns_empty_response_without_initialization_or_parsing(monkeypatch):
    from container_smoke import event

    def forbidden(*args, **kwargs):
        raise AssertionError("Preflight must not initialize services or parse a body")

    class NoEnvironment:
        get = __getitem__ = forbidden

    request = event("/v1/projects/synthetic/jobs", "OPTIONS")
    request.update(body="not base64 or JSON", isBase64Encoded=True)
    request["headers"] = {"origin": "https://untrusted.invalid"}
    with monkeypatch.context() as isolated:
        isolated.setattr(handler, "application", forbidden)
        isolated.setattr(handler, "site_auth", forbidden)
        isolated.setattr(handler.base64, "b64decode", forbidden)
        isolated.setattr(handler.json, "loads", forbidden)
        isolated.setattr(handler.os, "environ", NoEnvironment())
        response = handler.handler(request, None)
    assert response["statusCode"] == 204 and response["body"] == ""
    assert response["headers"]["cache-control"] == "no-store"
    assert not any(key.lower().startswith("access-control-") for key in response["headers"])
    assert "untrusted.invalid" not in json.dumps(response)


@pytest.mark.parametrize(
    "method,path,route_key",
    [
        ("GET", "/v1/me", "OPTIONS /v1/{proxy+}"),
        ("POST", "/v1/projects/synthetic/jobs", "OPTIONS /v1/{proxy+}"),
        ("OPTIONS", "/v1/me", "ANY /v1/{proxy+}"),
        ("OPTIONS", "/v1/me", "GET /v1/me"),
        ("OPTIONS", "/v1", "OPTIONS /v1/{proxy+}"),
        ("OPTIONS", "/health", "OPTIONS /v1/{proxy+}"),
    ],
)
def test_preflight_exception_cannot_bypass_actual_methods_or_routes(
    configured, monkeypatch, method, path, route_key
):
    from container_smoke import event

    def forbidden():
        raise AssertionError("Unauthenticated requests must not reach the application")

    monkeypatch.setattr(handler, "application", forbidden)
    request = event(path, method)
    request["routeKey"] = route_key
    request["body"] = "invalid JSON must be rejected after authentication"
    response = handler.handler(request, None)
    assert response["statusCode"] == 401
    assert json.loads(response["body"])["error"]["code"] == "unauthenticated"


def test_invalid_json_and_nonfinite_values(configured):
    for value in ("{", '{"unsafe":NaN}', '{"unsafe":Infinity}'):
        event = {**configured, "body": value}
        response = handler.handler(event, None)
        assert response["statusCode"] == 400
        assert "NaN" not in response["body"]
    assert (
        handler.handler({**configured, "body": "x" * 200_001}, None)["statusCode"]
        == 413
    )


def test_errors_are_json_and_do_not_expose_internal_details(configured, monkeypatch):
    class Unavailable:
        def handle(self, *args):
            raise OSError("sensitive internal details")

    monkeypatch.setattr(handler, "application", lambda: Unavailable())
    response = handler.handler(configured, None)
    assert response["statusCode"] == 503
    assert "sensitive" not in response["body"]
    assert response["headers"]["cache-control"] == "no-store"


def test_actual_application_initializes_and_serves_health_without_aws_calls(
    monkeypatch,
):
    boto3 = pytest.importorskip("boto3")
    from botocore.client import BaseClient
    from container_smoke import event

    environment = {
        "AWS_REGION": "eu-central-1",
        "AWS_DEFAULT_REGION": "eu-central-1",
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_CONFIG_FILE": "/dev/null",
        "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
        "STATE_TABLE": "local-smoke-table",
        "DATA_BUCKET": "local-smoke-bucket",
        "EVENT_QUEUE_URL": "http://127.0.0.1:9000/unused-queue",
        "COGNITO_ISSUER": "https://identity.invalid/local-smoke",
        "COGNITO_CLIENT_ID": "local-smoke-client",
    }
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)
    monkeypatch.setenv("RELEASE_POLICY", "nonproduction_legacy")
    monkeypatch.setattr(
        boto3,
        "DEFAULT_SESSION",
        boto3.Session(
            aws_access_key_id="local-smoke-placeholder",
            aws_secret_access_key="local-smoke-placeholder",
            region_name="eu-central-1",
        ),
    )

    def forbidden_aws_call(*args, **kwargs):
        raise AssertionError("The public health path must not call AWS")

    monkeypatch.setattr(BaseClient, "_make_api_call", forbidden_aws_call)
    monkeypatch.setattr(handler, "_application", None)
    healthy = handler.handler(event("/health"), None)
    assert healthy["statusCode"] == 200 and json.loads(healthy["body"]) == {
        "status": "ok"
    }
    assert handler._application.release_policy == "v1"
    config = handler._application.objects.client.meta.config
    assert config.connect_timeout == 2 and config.read_timeout == 3
    assert config.retries["total_max_attempts"] == 1
    denied = handler.handler(event("/v1/me"), None)
    assert denied["statusCode"] == 401
    assert json.loads(denied["body"])["error"]["code"] == "unauthenticated"


@pytest.fixture
def scheduled(monkeypatch):
    values = {
        "ARCHIVE_MAINTENANCE_RULE_ARN": "arn:aws:events:eu-central-1:123456789012:rule/private-archive-maintenance",
        "AWS_ACCOUNT_ID": "123456789012",
        "AWS_REGION": "eu-central-1",
        "COGNITO_ISSUER": "https://identity.invalid/pool",
        "COGNITO_CLIENT_ID": "browser-client",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return {
        "version": "0",
        "id": "local-scheduled-event",
        "detail-type": "Scheduled Event",
        "source": "aws.events",
        "account": values["AWS_ACCOUNT_ID"],
        "time": "2099-01-01T00:00:00Z",
        "region": values["AWS_REGION"],
        "resources": [values["ARCHIVE_MAINTENANCE_RULE_ARN"]],
        "detail": {},
    }


def test_exact_private_schedule_invokes_maintenance(scheduled, monkeypatch):
    class App:
        def maintenance(self, *, remaining_ms):
            assert remaining_ms() == 28000
            return {"status": "ok", "tagged": 3}

    class Context:
        def get_remaining_time_in_millis(self):
            return 28000

    monkeypatch.setattr(handler, "application", lambda: App())
    assert handler.handler(scheduled, Context()) == {"status": "ok", "tagged": 3}


@pytest.mark.parametrize(
    "field,value",
    [
        ("source", "untrusted.source"),
        ("account", "999999999999"),
        ("resources", ["arn:aws:events:eu-central-1:123456789012:rule/another-rule"]),
        ("region", "eu-west-1"),
        ("detail", {"operation": "archive"}),
        ("detail-type", "other-event"),
        ("version", "2.0"),
    ],
)
def test_mismatched_schedule_envelope_cannot_enter_private_maintenance(
    scheduled, monkeypatch, field, value
):
    def forbidden():
        raise AssertionError("Maintenance application must not be reached")

    monkeypatch.setattr(handler, "application", forbidden)
    response = handler.handler({**scheduled, field: value}, None)
    assert response["statusCode"] == 401


def test_http_api_envelope_cannot_fake_a_scheduled_invocation(scheduled, monkeypatch):
    def forbidden():
        raise AssertionError("HTTP request must not become private maintenance")

    monkeypatch.setattr(handler, "application", forbidden)
    forged = {
        **scheduled,
        "rawPath": "/v1/maintenance",
        "requestContext": {"http": {"method": "POST"}},
        "body": json.dumps(scheduled),
    }
    assert handler.handler(forged, None)["statusCode"] == 401


def test_authenticated_http_body_is_still_only_an_http_request(
    configured, scheduled, monkeypatch
):
    class App:
        def handle(self, method, path, claims, body):
            assert method == "GET" and path == "/v1/me" and body == scheduled
            return 200, {"subject": claims["sub"]}

        def maintenance(self, **kwargs):
            raise AssertionError("HTTP body must never invoke private maintenance")

    monkeypatch.setattr(handler, "application", lambda: App())
    response = handler.handler({**configured, "body": json.dumps(scheduled)}, None)
    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"subject": "alice"}


def test_schedule_time_is_not_the_retention_clock(scheduled, monkeypatch):
    from aws_publishing.api import Application
    from aws_publishing.storage import MemoryStore, MemoryObjects

    store, objects = MemoryStore(), MemoryObjects()
    store.state["artifacts"]["a"] = {
        "id": "a",
        "storage_key": "key",
        "archive_verified_at": 0,
    }
    app = Application(store, objects, clock=lambda: 0)
    monkeypatch.setattr(handler, "application", lambda: app)
    assert handler.handler(scheduled, None) == {
        "status": "ok",
        "tagged": 0,
        "remaining_due": False,
    }
    assert not objects.tags


def test_internal_maintenance_errors_remain_invocation_failures_for_retry(
    scheduled, monkeypatch
):
    class App:
        def maintenance(self, **kwargs):
            raise OSError("Retry private maintenance")

    monkeypatch.setattr(handler, "application", lambda: App())
    with pytest.raises(OSError, match="Retry private maintenance"):
        handler.handler(scheduled, None)


def test_http_reconciliation_receives_actual_lambda_time_budget(
    configured, monkeypatch
):
    class Context:
        def get_remaining_time_in_millis(self):
            return 16000

    class App:
        def handle(self, method, path, claims, body, *, remaining_ms):
            assert remaining_ms() == 16000
            return 200, {"subject": claims["sub"]}

    monkeypatch.setattr(handler, "application", lambda: App())
    assert handler.handler(configured, Context())["statusCode"] == 200


def test_remembered_web_client_is_explicitly_pinned_without_changing_native_client(configured,monkeypatch):
    monkeypatch.setenv("COGNITO_REMEMBERED_CLIENT_ID","remembered-web")
    assert handler.handler(configured,None)["statusCode"]==200
    request=copy.deepcopy(configured)
    request["requestContext"]["authorizer"]["jwt"]["claims"]["client_id"]="remembered-web"
    assert handler.handler(request,None)["statusCode"]==200
    request["requestContext"]["authorizer"]["jwt"]["claims"]["client_id"]="other-web"
    assert handler.handler(request,None)["statusCode"]==401
    request["requestContext"]["authorizer"]["jwt"]["claims"]["client_id"]="remembered-web"
    request["requestContext"]["authorizer"]["jwt"]["claims"]["token_use"]="id"
    assert handler.handler(request,None)["statusCode"]==401
