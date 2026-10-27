import json
from urllib.error import URLError
import pytest
from container_smoke import NoRedirect, event, invocation_url, invoke, smoke


def test_smoke_invokes_real_http_api_shapes_without_authorizer():
    requests = []

    def invoker(url, payload):
        assert url == "http://127.0.0.1:9000/2015-03-31/functions/function/invocations"
        assert payload["version"] == "2.0"
        method = payload["requestContext"]["http"]["method"]
        assert "authorizer" not in payload["requestContext"]
        assert "authorization" not in payload["headers"]
        requests.append((method, payload["rawPath"]))
        if method == "OPTIONS":
            assert payload["routeKey"] == "OPTIONS /v1/{proxy+}"
            assert payload["headers"]["access-control-request-method"] == "GET"
            return {"statusCode": 204, "body": "", "headers": {"cache-control": "no-store"}}
        if payload["rawPath"] == "/health":
            return {
                "statusCode": 200,
                "body": '{"status":"ok"}',
                "headers": {"cache-control": "no-store"},
            }
        return {"statusCode": 401, "body": '{"error":{"code":"unauthenticated"}}'}

    smoke("http://127.0.0.1:9000", invoker=invoker)
    assert requests == [
        ("OPTIONS", "/v1/me"),
        ("GET", "/health"),
        ("GET", "/v1/me"),
        ("POST", "/v1/projects/synthetic/jobs"),
    ]


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://example.com",
        "http://127.0.0.1:9000/path",
        "http://user:password@localhost:9000",
        "http://localhost:9000?token=secret",
    ],
)
def test_container_smoke_cannot_target_external_services(url):
    with pytest.raises(ValueError, match="loopback"):
        invocation_url(url)


def test_invocation_retries_startup_then_posts_http_api_event():
    calls, pauses = [], []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            assert limit == 100_000
            return b'{"statusCode":200,"body":"{\\"status\\":\\"ok\\"}"}'

    def opener(request, timeout):
        assert timeout == 2
        assert request.method == "POST"
        assert json.loads(request.data)["rawPath"] == "/health"
        calls.append(request)
        if len(calls) == 1:
            raise URLError("not listening yet")
        return Response()

    result = invoke(
        invocation_url("http://localhost:9000"),
        event("/health"),
        opener=opener,
        clock=lambda: 0,
        pause=pauses.append,
    )
    assert result["statusCode"] == 200 and pauses == [0.25]


def test_invocation_readiness_has_a_deadline():
    times = iter([0, 46])

    def unavailable(*args, **kwargs):
        raise URLError("not ready")

    with pytest.raises(TimeoutError, match="deadline"):
        invoke(
            invocation_url("http://localhost:9000"),
            event("/health"),
            opener=unavailable,
            clock=lambda: next(times),
            pause=lambda _: None,
        )


def test_lambda_initialization_error_is_a_failure():
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            return b'{"errorMessage":"Runtime.ImportModuleError"}'

    with pytest.raises(AssertionError, match="invocation error"):
        invoke(
            invocation_url("http://localhost:9000"),
            event("/health"),
            opener=lambda *args, **kwargs: Response(),
        )


def test_local_emulator_cannot_redirect_the_smoke_to_external_services():
    with pytest.raises(ValueError, match="redirect"):
        NoRedirect().redirect_request(
            None, None, 302, "Found", {}, "https://example.com"
        )
