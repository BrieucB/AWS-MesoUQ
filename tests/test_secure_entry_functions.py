"""Offline edge contracts; native enforcement is checked separately after publish."""
import copy
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("edge_fixtures", ROOT / "tests/test_remembered_entry_function.py")
fixtures = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixtures)


class SecureEntryTests(fixtures.RememberedEntryTests):
    def run_source(self, source, requests):
        original = fixtures.SOURCE
        try:
            fixtures.SOURCE = ROOT / ("infra/aws_publishing/functions/" + source)
            return super().run_requests(requests)
        finally:
            fixtures.SOURCE = original

    def run_requests(self, requests):
        return self.run_source("private-entry.js", requests)

    def test_private_alias_only_rewrites_exact_get_head_preserving_all_request_data(self):
        requests = [{"uri": "/app", "method": method, "cookies": {},
                     "querystring": {"state": {"value": "synthetic"}},
                     "headers": {"host": {"value": "untrusted.example"}}}
                    for method in ("GET", "HEAD")]
        original = copy.deepcopy(requests)
        results = self.run_requests(requests)
        for request, result in zip(original, results):
            self.assertTrue(result["unchanged"])
            request["uri"] = "/index.html"
            self.assertEqual(result["result"], request)

    def test_similar_alias_paths_and_other_methods_are_unchanged(self):
        requests = [{"uri": uri, "method": "GET", "cookies": {}}
                    for uri in ("/app/", "/app.js", "//app", "/%61pp", "/APP")]
        requests += [{"uri": "/app", "method": method}
                     for method in ("POST", "PUT", "OPTIONS")]
        for result, original in zip(self.run_requests(requests), requests):
            self.assertTrue(result["unchanged"])
            self.assertEqual(result["result"], original)

    def test_public_redirect_ignores_all_incoming_cookie_query_and_host_data(self):
        requests = [{"uri": uri, "method": method, "cookies": cookies,
                     "querystring": {"redirect": {"value": "https://untrusted.example"}},
                     "headers": {"host": {"value": "untrusted.example"}}}
                    for uri in ("/", "/index.html") for method in ("GET", "HEAD")
                    for cookies in ({}, {name: {"value": "expired"} for name in fixtures.NAMES})]
        for response in self.run_source("public-entry.js", requests):
            self.assertEqual(response["result"], {
                "statusCode": 302, "statusDescription": "Found",
                "headers": {"location": {"value": "/auth/login"},
                            "cache-control": {"value": "no-store"}}})


if __name__ == "__main__":
    unittest.main()
