"""Synthetic metadata only; no AWS, credentials or network calls."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import prepare_cold_entry_release as release

ACCOUNT = "000000000000"
ARN = "arn:aws:cloudfront::" + ACCOUNT + ":distribution/" + release.DISTRIBUTION_ID


def function(kind):
    suffix = "remembered-entry" if kind == "legacy" else release.FUNCTIONS[kind] + "-v1"
    return {"ARN": "arn:aws:cloudfront::" + ACCOUNT + ":function/aws-mesouq-" + suffix,
            "ETag": kind + "-etag", "Stage": "LIVE", "Runtime": "cloudfront-js-1.0",
            "Code": (ROOT / ("infra/aws_publishing/functions/" + release.FUNCTIONS[kind] + ".js")).read_text()}


def metadata():
    methods = {"Quantity": 2, "Items": ["HEAD", "GET"],
               "CachedMethods": {"Quantity": 2, "Items": ["HEAD", "GET"]}}
    default = {"TargetOriginId": "private-web", "TrustedKeyGroups": {"Enabled": True, "Quantity": 1, "Items": ["key-group"]},
        "ViewerProtocolPolicy": "redirect-to-https", "AllowedMethods": methods,
        "CachePolicyId": "no-cache", "ResponseHeadersPolicyId": "security-headers",
        "FunctionAssociations": {"Quantity": 1, "Items": [{"EventType": "viewer-request", "FunctionARN": function("legacy")["ARN"]}]}}
    config = {"CallerReference": "retain", "DefaultRootObject": "index.html", "Enabled": True,
        "DefaultCacheBehavior": default,
        "Origins": {"Quantity": 2, "Items": [
            {"Id": "private-web", "DomainName": "fixture-web.s3.eu-central-1.amazonaws.com", "OriginPath": "", "OriginAccessControlId": "oac"},
            {"Id": "regional-auth", "DomainName": "abc123.execute-api.eu-central-1.amazonaws.com", "OriginPath": "",
             "CustomOriginConfig": {"OriginProtocolPolicy": "https-only"}, "CustomHeaders": {"Quantity": 0}}]},
        "CacheBehaviors": {"Quantity": 1, "Items": [
            {"PathPattern": "/auth/*", "TargetOriginId": "regional-auth", "CachePolicyId": "no-cache",
             "OriginRequestPolicyId": "auth-forwarding-preserved", "AllowedMethods": {"Quantity": 7, "Items": ["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"]},
             "FunctionAssociations": {"Quantity": 0}, "ResponseHeadersPolicyId": "security-headers"}]}}
    s3 = {"Bucket": "fixture-web", "PublicAccessBlockConfiguration": {
        "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True},
        "OriginAccessControl": {"Id": "oac", "OriginAccessControlConfig": {
            "Name": "fixture", "SigningProtocol": "sigv4", "SigningBehavior": "always", "OriginAccessControlOriginType": "s3"}},
        "Policy": {"Version": "2012-10-17", "Statement": [
            {"Sid": "CloudFrontOnly", "Effect": "Allow", "Principal": {"Service": "cloudfront.amazonaws.com"},
             "Action": "s3:GetObject", "Resource": "arn:aws:s3:::fixture-web/*", "Condition": {"StringEquals": {"AWS:SourceArn": ARN}}},
            {"Sid": "TLSOnly", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
             "Resource": ["arn:aws:s3:::fixture-web", "arn:aws:s3:::fixture-web/*"],
             "Condition": {"Bool": {"aws:SecureTransport": "false"}}}]}}
    return {"schema": release.SCHEMA, "reviewed_source_commit": "a" * 40,
        "static_baseline_commit": release.BASELINE, "operation": "private-alias",
        "distribution": {"Id": release.DISTRIBUTION_ID, "ARN": ARN, "ETag": "fresh-etag", "Status": "Deployed", "DistributionConfig": copy.deepcopy(config)},
        "initial_distribution_config": config, "functions": {key: function(key) for key in release.FUNCTIONS},
        "auth_api": {"ApiId": "abc123", "ApiEndpoint": "https://abc123.execute-api.eu-central-1.amazonaws.com", "ProtocolType": "HTTP",
            "Routes": [{"RouteId": "route123", "RouteKey": "ANY /auth/{proxy+}", "Target": "integrations/int123", "AuthorizationType": "NONE"}]},
        "cache_policy": {"Id": "no-cache", "CachePolicyConfig": {"Name": "fixture", "MinTTL": 0, "MaxTTL": 0, "DefaultTTL": 0,
            "ParametersInCacheKeyAndForwardedToOrigin": {
                "EnableAcceptEncodingGzip": False, "EnableAcceptEncodingBrotli": False,
                "HeadersConfig": {"HeaderBehavior": "none"}, "CookiesConfig": {"CookieBehavior": "none"},
                "QueryStringsConfig": {"QueryStringBehavior": "none"}}}},
        "s3": s3, "application_proof": None}


class ColdEntryPlanTests(unittest.TestCase):
    def setUp(self):
        self.value = metadata()
        self.source = patch.object(release, "verify_source", return_value=None)
        self.source.start()
        self.addCleanup(self.source.stop)
        self.initial = patch.object(release, "INITIAL_SHA256", release.sha(self.value["initial_distribution_config"]))
        self.initial.start()
        self.addCleanup(self.initial.stop)

    def alias(self):
        return release.prepare(self.value)["request"]["DistributionConfig"]

    def public_input(self):
        value = copy.deepcopy(self.value)
        value["distribution"]["DistributionConfig"] = self.alias()
        value["operation"] = "public-entry"
        value["application_proof"] = {"source_commit": value["reviewed_source_commit"],
            "backend_revision_id": "deployed-revision", "backend_image_sha256": "b" * 64,
            "app_js_sha256": "c" * 64, "report_sha256": "d" * 64,
            "alias_config_sha256": release.sha(value["distribution"]["DistributionConfig"])}
        return value

    def test_alias_changes_only_default_hook_and_preserves_input(self):
        original = copy.deepcopy(self.value)
        result = release.prepare(self.value)
        self.assertEqual(self.value, original)
        candidate = result["request"]["DistributionConfig"]
        before = original["distribution"]["DistributionConfig"]
        left, right = copy.deepcopy(candidate), copy.deepcopy(before)
        left["DefaultCacheBehavior"].pop("FunctionAssociations")
        right["DefaultCacheBehavior"].pop("FunctionAssociations")
        self.assertEqual(left, right)
        self.assertEqual(result["request"]["IfMatch"], "fresh-etag")
        self.assertFalse(result["aws_calls_made"])
        self.assertTrue(result["rollback_guard"]["recapture_live_etag_required"])

    def test_public_only_inserts_exact_api_behaviors_and_clears_root_in_same_cas(self):
        value = self.public_input()
        candidate = release.prepare(value)["request"]["DistributionConfig"]
        self.assertEqual(candidate["DefaultRootObject"], "")
        self.assertEqual(candidate["DefaultCacheBehavior"], value["distribution"]["DistributionConfig"]["DefaultCacheBehavior"])
        entries = candidate["CacheBehaviors"]["Items"]
        self.assertEqual([item["PathPattern"] for item in entries], ["/", "/index.html", "/auth/*"])
        self.assertEqual(entries[2], value["initial_distribution_config"]["CacheBehaviors"]["Items"][0])
        for item in entries[:2]:
            self.assertEqual(item["TargetOriginId"], "regional-auth")
            self.assertEqual(set(item["AllowedMethods"]["Items"]), {"GET", "HEAD"})
            self.assertNotIn("OriginRequestPolicyId", item)
            self.assertFalse(item["TrustedKeyGroups"]["Enabled"])

    def test_reverse_cas_requires_fresh_etag_and_restores_exact_before_images(self):
        public_input = self.public_input()
        public = release.prepare(public_input)["request"]["DistributionConfig"]
        reverse = copy.deepcopy(public_input)
        reverse["operation"] = "rollback-public"
        reverse["application_proof"] = None
        reverse["distribution"].update(ETag="new-after-deploy", DistributionConfig=public)
        result = release.prepare(reverse)
        self.assertEqual(result["request"]["IfMatch"], "new-after-deploy")
        self.assertEqual(result["request"]["DistributionConfig"], self.alias())
        reverse["operation"] = "rollback-alias"
        reverse["distribution"].update(ETag="new-after-reverse-c", DistributionConfig=self.alias())
        self.assertEqual(release.prepare(reverse)["request"]["DistributionConfig"], self.value["initial_distribution_config"])

    def test_unknown_fields_wrong_etags_and_account_or_function_drift_are_rejected(self):
        changes = [
            lambda v: v.update(unreviewed=True),
            lambda v: v["distribution"].update(ETag=""),
            lambda v: v["distribution"].update(ARN=ARN.replace(ACCOUNT, "111111111111")),
            lambda v: v["functions"]["private"].update(Code="unreviewed"),
            lambda v: v["functions"]["public"].update(ETag=""),
            lambda v: v["functions"]["legacy"].update(Runtime="cloudfront-js-2.0"),
            lambda v: v["auth_api"]["Routes"][0].update(unreviewed=True),
            lambda v: v["cache_policy"].update(unreviewed=True),
        ]
        for change in changes:
            with self.subTest(change=changes.index(change)):
                value = copy.deepcopy(self.value)
                change(value)
                with self.assertRaises(ValueError):
                    release.prepare(value)

    def test_live_topology_drift_and_unreviewed_initial_bytes_are_rejected(self):
        for path in ("current", "initial"):
            value = copy.deepcopy(self.value)
            cfg = value["distribution"]["DistributionConfig"] if path == "current" else value["initial_distribution_config"]
            cfg["DefaultCacheBehavior"]["TrustedKeyGroups"]["Enabled"] = False
            with self.assertRaises(ValueError):
                release.prepare(value)

    def test_api_default_route_origin_rewrite_forwarding_and_s3_permissions_fail(self):
        changes = [
            lambda v: v["auth_api"]["Routes"].append(dict(v["auth_api"]["Routes"][0], RouteKey="$default")),
            lambda v: v["auth_api"]["Routes"][0].update(RouteKey="ANY /{proxy+}"),
            lambda v: v["auth_api"].update(ApiEndpoint="https://untrusted.example"),
            lambda v: v["cache_policy"]["CachePolicyConfig"].update(MaxTTL=10),
            lambda v: v["cache_policy"]["CachePolicyConfig"]["ParametersInCacheKeyAndForwardedToOrigin"].update(CookiesConfig={"CookieBehavior": "all"}),
            lambda v: v["s3"]["PublicAccessBlockConfiguration"].update(BlockPublicPolicy=False),
            lambda v: v["s3"]["Policy"]["Statement"][0].update(Action=["s3:GetObject", "s3:ListBucket"]),
            lambda v: v["s3"]["OriginAccessControl"]["OriginAccessControlConfig"].update(SigningBehavior="never"),
        ]
        for change in changes:
            with self.subTest(change=changes.index(change)):
                value = copy.deepcopy(self.value)
                change(value)
                with self.assertRaises(ValueError):
                    release.prepare(value)

    def test_stage_c_requires_exact_source_alias_and_application_proof(self):
        for mode in ("none", "source", "alias", "digest", "extra"):
            value = self.public_input()
            if mode == "none":
                value["application_proof"] = None
            elif mode == "source":
                value["application_proof"]["source_commit"] = "e" * 40
            elif mode == "alias":
                value["application_proof"]["alias_config_sha256"] = "e" * 64
            elif mode == "digest":
                value["application_proof"]["app_js_sha256"] = "not-a-digest"
            else:
                value["application_proof"]["extra"] = "unexpected"
            with self.assertRaises(ValueError):
                release.prepare(value)

    def test_duplicate_json_keys_are_rejected(self):
        with self.assertRaises(ValueError):
            release.no_duplicates([("schema", "one"), ("schema", "two")])


class SourceFreezeTests(unittest.TestCase):
    def test_wrong_commit_and_dirty_code_are_rejected_before_any_plan(self):
        with self.assertRaises(ValueError):
            release.verify_source("main")
        with patch.object(release.subprocess, "check_output", return_value="b" * 40):
            with self.assertRaisesRegex(ValueError, "reviewed-source-mismatch"):
                release.verify_source("a" * 40)
        def dirty(args, **kwargs):
            return "a" * 40 if "rev-parse" in args else b"dirty"
        with patch.object(release.subprocess, "check_output", side_effect=dirty), \
                patch.object(release.subprocess, "check_call", return_value=0):
            with self.assertRaisesRegex(ValueError, "dirty-release-source"):
                release.verify_source("a" * 40)


if __name__ == "__main__":
    unittest.main()
