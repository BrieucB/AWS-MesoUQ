"""Prepare an offline, owner-reviewable remembered-browser activation request.

This command makes no AWS calls and has no apply mode. Input is nonsecret AWS
resource metadata, never tokens, cookies, credential files or session records.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(",",":"),allow_nan=False).encode()


def sha(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def prepare(metadata):
    required={"reviewed_source_commit","function_config","native_client_before",
        "native_client_after","remembered_client","authorizer","table_ttl","user_pool",
        "distribution","entry_function","entry_function_code"}
    if not isinstance(metadata,dict) or set(metadata)!=required:
        raise ValueError("Exact release metadata fields required")
    source=metadata["reviewed_source_commit"]
    if not isinstance(source,str) or re.fullmatch("[a-f0-9]{40}",source) is None:
        raise ValueError("Exact reviewed source commit required")
    config=metadata["function_config"]
    env=config["Environment"]["Variables"]
    before=metadata["native_client_before"]["UserPoolClient"]
    after=metadata["native_client_after"]["UserPoolClient"]
    web=metadata["remembered_client"]["UserPoolClient"]
    if before!=after or "ClientSecret" in before or "ClientSecret" in web:
        raise ValueError("Original public connector client must remain unchanged")
    if before["ClientId"]!=env["COGNITO_CLIENT_ID"] or web["ClientId"]==before["ClientId"]:
        raise ValueError("Separate remembered-web and original client IDs required")
    if before["UserPoolId"]!=web["UserPoolId"]:
        raise ValueError("Remembered client must use the existing MFA user pool")
    pool=metadata["user_pool"]["UserPool"]
    if pool["Id"]!=web["UserPoolId"] or pool["MfaConfiguration"]!="ON":
        raise ValueError("Existing required MFA must remain enabled")
    base=env["SITE_BASE_URL"].rstrip("/")
    if not base.startswith("https://") or "/" in base[8:]:
        raise ValueError("Exact HTTPS site origin required")
    if (web["CallbackURLs"]!=[base+"/auth/callback"] or
        web["LogoutURLs"]!=[base+"/auth/login"] or
        web["AllowedOAuthFlows"]!=["code"] or
        web["AllowedOAuthFlowsUserPoolClient"] is not True or
        sorted(web["AllowedOAuthScopes"])!=["email","openid","profile"] or
        web["SupportedIdentityProviders"]!=["COGNITO"] or
        web["EnableTokenRevocation"] is not True or
        web["ExplicitAuthFlows"]!=["ALLOW_REFRESH_TOKEN_AUTH"] or
        web["AccessTokenValidity"]!=15 or web["IdTokenValidity"]!=15 or
        web["RefreshTokenValidity"]!=30 or
        web["TokenValidityUnits"]!={"AccessToken":"minutes","IdToken":"minutes","RefreshToken":"days"}):
        raise ValueError("Remembered client must match the reviewed web-only 15-minute/30-day policy")
    jwt=metadata["authorizer"]["JwtConfiguration"]
    if (sorted(jwt["Audience"])!=sorted([before["ClientId"],web["ClientId"]]) or
            jwt["Issuer"]!=env["COGNITO_ISSUER"]):
        raise ValueError("API authorizer must pin exactly the old and remembered clients")
    ttl=metadata["table_ttl"]["TimeToLiveDescription"]
    if ttl.get("TimeToLiveStatus")!="ENABLED" or ttl.get("AttributeName")!="auth_expires_at":
        raise ValueError("Auth-only TTL cleanup must be enabled before activation")
    if (env.get("COGNITO_REMEMBERED_CLIENT_ID")!=web["ClientId"] or
            env.get("SITE_REMEMBERED_SESSIONS_ENABLED","false")!="false"):
        raise ValueError("Activation requires the configured dedicated client and disabled before-image")
    if not isinstance(config.get("RevisionId"),str) or not config["RevisionId"]:
        raise ValueError("Lambda revision precondition required")
    summary=metadata["entry_function"]["FunctionSummary"]
    expected_code=(Path(__file__).resolve().parents[1]/
        "infra/aws_publishing/functions/remembered-entry.js").read_bytes()
    downloaded=metadata["entry_function_code"]
    if (not isinstance(downloaded,str) or downloaded.encode()!=expected_code or
        summary["FunctionMetadata"]["Stage"]!="LIVE" or
        summary["FunctionConfig"]["Runtime"]!="cloudfront-js-1.0" or
        not metadata["entry_function"].get("ETag")):
        raise ValueError("Published viewer-entry function must match the exact reviewed source")
    function_arn=summary["FunctionMetadata"]["FunctionARN"]
    account=re.fullmatch(r"arn:aws:lambda:[a-z0-9-]+:([0-9]{12}):function:[A-Za-z0-9_-]+",
        config["FunctionArn"])
    if account is None or not re.fullmatch(
            r"arn:aws:cloudfront::"+account[1]+r":function/[A-Za-z0-9_-]+",function_arn):
        raise ValueError("Entry function must belong to the reviewed existing account")
    distribution=metadata["distribution"]["Distribution"]
    original=distribution["DistributionConfig"]
    if not metadata["distribution"].get("ETag") or distribution["Status"]!="Deployed":
        raise ValueError("Exact deployed distribution ETag required")
    trusted=original["DefaultCacheBehavior"]["TrustedKeyGroups"]
    if trusted.get("Enabled") is not True or trusted.get("Quantity",0)<1 or not trusted.get("Items"):
        raise ValueError("Default assets must retain native signed-grant authorization")
    desired=copy.deepcopy(original)
    associations=desired["DefaultCacheBehavior"].get("FunctionAssociations",{"Quantity":0})
    items=copy.deepcopy(associations.get("Items",[]))
    if associations.get("Quantity")!=len(items):
        raise ValueError("Consistent existing viewer-function associations required")
    viewers=[item for item in items if item["EventType"]=="viewer-request"]
    target={"EventType":"viewer-request","FunctionARN":function_arn}
    if viewers and viewers!=[target]:
        raise ValueError("Existing viewer-request hook requires separate reviewed integration")
    if not viewers:
        items.append(target)
        desired["DefaultCacheBehavior"]["FunctionAssociations"]={"Quantity":len(items),"Items":items}
    # Every origin, signed key group, cache/header policy and private asset rule is
    # preserved byte-for-byte except this one allowed association field.
    left=copy.deepcopy(original);right=copy.deepcopy(desired)
    left["DefaultCacheBehavior"].pop("FunctionAssociations",None)
    right["DefaultCacheBehavior"].pop("FunctionAssociations",None)
    if left!=right:
        raise ValueError("Unexpected distribution security change")
    candidate=dict(env,SITE_REMEMBERED_SESSIONS_ENABLED="true")
    return {"schema":"aws_mesouq.remembered_browser_activation.v1",
        "reviewed_source_commit":source,"expected_environment_sha256":sha(env),
        "candidate_environment_sha256":sha(candidate),"native_client_sha256":sha(before),
        "remembered_client_sha256":sha(web),
        "entry_function_code_sha256":hashlib.sha256(expected_code).hexdigest(),
        "expected_entry_function_etag":metadata["entry_function"]["ETag"],
        "expected_distribution_etag":metadata["distribution"]["ETag"],
        "expected_distribution_config_sha256":sha(original),
        "candidate_distribution_config_sha256":sha(desired),
        "distribution_change_paths":[] if viewers else ["DefaultCacheBehavior.FunctionAssociations"],
        "distribution_activation_request":None if viewers else {
            "Id":distribution["Id"],"IfMatch":metadata["distribution"]["ETag"],
            "DistributionConfig":desired},
        "activation_request":{"FunctionName":config["FunctionName"],
            "RevisionId":config["RevisionId"],"Environment":{"Variables":candidate}},
        "resource_changes":["one public remembered-web app client",
            "existing API authorizer adds the remembered client audience",
            "existing table TTL enables auth_expires_at",
            "one reviewed CloudFront viewer-entry function and conditional association",
            "one Lambda environment flag enables remembered sessions"],
        "existing_business_iam_preserved":True,"new_aws_services":False,
        "owner_action_approval_required":True,"aws_calls_made":False,
        "ordering_unverified_until_live":True,
        "mandatory_live_cold_entry_checks":["GET / without signed grants","GET /index.html without signed grants",
            "HEAD / without signed grants","HEAD /index.html without signed grants",
            "private assets still require native signed grants"]}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    if args.metadata.stat().st_size>262144:
        parser.error("Bounded nonsecret resource metadata required")
    candidate=prepare(json.loads(args.metadata.read_text()))
    path=args.output.absolute()
    if path.parent.resolve()!=path.parent or path.is_symlink():
        parser.error("Canonical private output path required")
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,"wb") as stream:
        stream.write(canonical(candidate)+b"\n");stream.flush();os.fsync(stream.fileno())
    print(json.dumps({key:candidate[key] for key in
        ("schema","reviewed_source_commit","expected_environment_sha256",
         "candidate_environment_sha256","owner_action_approval_required","aws_calls_made")}))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
