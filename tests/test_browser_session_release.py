"""Offline activation metadata guards: no credentials, AWS calls or apply path."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"tools"))
import prepare_browser_session_release as release


def metadata():
    native={"ClientId":"original","UserPoolId":"pool","RefreshTokenValidity":1,
        "TokenValidityUnits":{"RefreshToken":"days"}}
    web={"ClientId":"remembered","UserPoolId":"pool",
        "CallbackURLs":["https://portal.example/auth/callback"],
        "LogoutURLs":["https://portal.example/auth/login"],"AllowedOAuthFlows":["code"],
        "AllowedOAuthFlowsUserPoolClient":True,"AllowedOAuthScopes":["openid","email","profile"],
        "SupportedIdentityProviders":["COGNITO"],"EnableTokenRevocation":True,
        "ExplicitAuthFlows":["ALLOW_REFRESH_TOKEN_AUTH"],
        "AccessTokenValidity":15,"IdTokenValidity":15,"RefreshTokenValidity":30,
        "TokenValidityUnits":{"AccessToken":"minutes","IdToken":"minutes","RefreshToken":"days"}}
    return {"reviewed_source_commit":"a"*40,
        "function_config":{"FunctionName":"fixture","FunctionArn":"arn:aws:lambda:eu-central-1:000000000000:function:fixture",
            "RevisionId":"before-revision",
            "Environment":{"Variables":{"COGNITO_CLIENT_ID":"original","COGNITO_REMEMBERED_CLIENT_ID":"remembered",
                "SITE_REMEMBERED_SESSIONS_ENABLED":"false","COGNITO_ISSUER":"https://issuer.example/pool","SITE_BASE_URL":"https://portal.example",
                "BENCHMARK_POLICIES":"preserve-exactly","KEEP":"private-nonsecret-config"}}},
        "native_client_before":{"UserPoolClient":copy.deepcopy(native)},
        "native_client_after":{"UserPoolClient":copy.deepcopy(native)},
        "remembered_client":{"UserPoolClient":web},
        "authorizer":{"JwtConfiguration":{"Audience":["original","remembered"],"Issuer":"https://issuer.example/pool"}},
        "table_ttl":{"TimeToLiveDescription":{"TimeToLiveStatus":"ENABLED","AttributeName":"auth_expires_at"}},
        "user_pool":{"UserPool":{"Id":"pool","MfaConfiguration":"ON"}},
        "entry_function_code":(Path(__file__).resolve().parents[1]/"infra/aws_publishing/functions/remembered-entry.js").read_text(),
        "entry_function":{"ETag":"function-before","FunctionSummary":{
            "FunctionMetadata":{"Stage":"LIVE","FunctionARN":"arn:aws:cloudfront::000000000000:function/remembered-entry"},
            "FunctionConfig":{"Runtime":"cloudfront-js-1.0"}}},
        "distribution":{"ETag":"distribution-before","Distribution":{"Id":"FIXTURE","Status":"Deployed",
            "DistributionConfig":{"DefaultRootObject":"index.html",
                "DefaultCacheBehavior":{"TargetOriginId":"private-web",
                    "TrustedKeyGroups":{"Enabled":True,"Quantity":1,"Items":["trusted-key-group"]},
                    "FunctionAssociations":{"Quantity":0}},
                "Origins":{"Quantity":1,"Items":[{"Id":"private-web","DomainName":"private.s3.example"}]}}}}}


class SessionReleaseTests(unittest.TestCase):
    def test_prepares_one_disabled_to_enabled_environment_delta_without_mutating_inputs(self):
        value=metadata();before=copy.deepcopy(value);result=release.prepare(value)
        self.assertEqual(value,before)
        request=result["activation_request"]
        self.assertEqual(request["RevisionId"],"before-revision")
        updated=request["Environment"]["Variables"]
        self.assertEqual(updated["COGNITO_CLIENT_ID"],"original")
        self.assertEqual(updated["COGNITO_REMEMBERED_CLIENT_ID"],"remembered")
        self.assertEqual(updated["SITE_REMEMBERED_SESSIONS_ENABLED"],"true")
        self.assertEqual(updated["BENCHMARK_POLICIES"],"preserve-exactly")
        self.assertEqual(updated["KEEP"],"private-nonsecret-config")
        self.assertTrue(result["owner_action_approval_required"])
        self.assertFalse(result["aws_calls_made"])
        self.assertFalse(result["new_aws_services"])
        self.assertTrue(result["ordering_unverified_until_live"])

    def test_rejects_changed_native_refresh_lifetime(self):
        value=metadata();value["native_client_after"]["UserPoolClient"]["RefreshTokenValidity"]=30
        with self.assertRaisesRegex(ValueError,"unchanged"):release.prepare(value)

    def test_rejects_new_client_secret_or_nonweb_callback(self):
        for change in ("secret","callback"):
            value=metadata();client=value["remembered_client"]["UserPoolClient"]
            if change=="secret":client["ClientSecret"]="synthetic-forbidden"
            else:client["CallbackURLs"].append("http://localhost:8765/callback")
            with self.assertRaises(ValueError):release.prepare(value)

    def test_rejects_lifetime_audience_issuer_mfa_and_ttl_drift(self):
        changes=[
            lambda v:v["remembered_client"]["UserPoolClient"].update(RefreshTokenValidity=365),
            lambda v:v["authorizer"]["JwtConfiguration"].update(Audience=["original","untrusted"]),
            lambda v:v["authorizer"]["JwtConfiguration"].update(Issuer="https://other.example"),
            lambda v:v["user_pool"]["UserPool"].update(MfaConfiguration="OFF"),
            lambda v:v["table_ttl"]["TimeToLiveDescription"].update(AttributeName="payload"),
            lambda v:v["function_config"]["Environment"]["Variables"].update(SITE_REMEMBERED_SESSIONS_ENABLED="true")]
        for change in changes:
            with self.subTest(change=changes.index(change)):
                value=metadata();change(value)
                with self.assertRaises(ValueError):release.prepare(value)


    def test_distribution_delta_is_only_exact_viewer_hook_and_preserves_private_keys(self):
        value=metadata();before=copy.deepcopy(value["distribution"])
        result=release.prepare(value)
        self.assertEqual(value["distribution"],before)
        self.assertEqual(result["expected_distribution_etag"],"distribution-before")
        self.assertEqual(result["expected_entry_function_etag"],"function-before")
        self.assertEqual(result["distribution_change_paths"],["DefaultCacheBehavior.FunctionAssociations"])
        candidate=result["distribution_activation_request"]["DistributionConfig"]
        original=before["Distribution"]["DistributionConfig"]
        left=copy.deepcopy(candidate);right=copy.deepcopy(original)
        left["DefaultCacheBehavior"].pop("FunctionAssociations")
        right["DefaultCacheBehavior"].pop("FunctionAssociations")
        self.assertEqual(left,right)
        self.assertTrue(candidate["DefaultCacheBehavior"]["TrustedKeyGroups"]["Enabled"])

    def test_rejects_function_code_drift_unknown_hook_or_unsigned_private_default(self):
        for change in ("code","hook","unsigned"):
            value=metadata()
            if change=="code":value["entry_function_code"]+="// unreviewed"
            elif change=="hook":
                value["distribution"]["Distribution"]["DistributionConfig"]["DefaultCacheBehavior"]["FunctionAssociations"]={
                    "Quantity":1,"Items":[{"EventType":"viewer-request","FunctionARN":"other-hook"}]}
            else:
                value["distribution"]["Distribution"]["DistributionConfig"]["DefaultCacheBehavior"]["TrustedKeyGroups"]["Enabled"]=False
            with self.assertRaises(ValueError):release.prepare(value)


    def test_rejects_malformed_or_cross_account_cloudfront_function_arn(self):
        for arn in ("arn:aws:cloudfront::000000000000:function:remembered-entry",
                    "arn:aws:cloudfront::111111111111:function/remembered-entry"):
            value=metadata()
            value["entry_function"]["FunctionSummary"]["FunctionMetadata"]["FunctionARN"]=arn
            with self.assertRaisesRegex(ValueError,"existing account"):
                release.prepare(value)
