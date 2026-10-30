"""Public session regressions runnable against the exact pre-feature backend."""
import json
from test_site_auth import auth, event


def test_login_exposes_explicit_default_off_remember_choice(auth):
    response = auth.handle(event("/auth/login"))
    assert response["statusCode"] == 200
    assert response["headers"]["content-type"].startswith("text/html")
    assert 'name="remember"' in response["body"]
    assert "Keep me signed in" in response["body"]
    assert " checked" not in response["body"]
    assert "access-secret" not in response["body"]


def test_refresh_route_rejects_cross_origin_before_any_token_use(auth):
    request = event("/auth/refresh", "POST",
        headers={"Origin":"https://other.example","Content-Type":"application/json"})
    request["body"] = json.dumps({"activity":False})
    response = auth.handle(request)
    assert response["statusCode"] == 403
    assert not auth.cognito.calls
