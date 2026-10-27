"""Loopback-only development server. Explicit local identity; never deploy this.

Run PYTHONPATH=services/aws_publishing:src /usr/bin/python3.11 -m aws_publishing.local
Production authentication only exists in handler.py behind API Gateway JWT.
"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from .api import Application, APIError
from .storage import MemoryObjects, MemoryStore


def main():
    from aws_mesouq.contracts import validate_job_configuration, validate_result

    store, objects = MemoryStore(), MemoryObjects()
    store.state["projects"]["demo"] = {"name": "Local demo"}
    store.state["identities"]["local-user"] = {
        "email": "local@example.invalid",
        "projects": {"demo": "maintainer"},
    }
    app = Application(
        store,
        objects,
        validate_configuration=validate_job_configuration,
        validate_result=validate_result,
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.dispatch()

        def do_POST(self):
            self.dispatch()

        def log_message(self, *args):
            pass

        def dispatch(self):
            try:
                length = int(self.headers.get("content-length", 0))
                if length > 200_000:
                    raise APIError(413, "request_too_large", "Request too large")
                body = json.loads(self.rfile.read(length) or "{}")
                status, result = app.handle(
                    self.command, self.path, {"sub": "local-user"}, body
                )
            except APIError as exc:
                status, result = exc.status, {
                    "error": {"code": exc.code, "message": exc.message}
                }
            except (ValueError, TypeError):
                status, result = 400, {
                    "error": {"code": "invalid_request", "message": "Invalid JSON"}
                }
            payload = json.dumps(result).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    HTTPServer(("127.0.0.1", 8765), Handler).serve_forever()


if __name__ == "__main__":
    main()
