"""Gateway-authorized HTTP API and IAM-scoped scheduled archive maintenance."""

import base64
import json
import logging
import os
from .api import Application, APIError
from .storage import DynamoStore, S3Objects, SQSNotifications

LOG = logging.getLogger(__name__)
_application = None
_site_auth = None
_benchmark_adapter = None


def application():
    global _application
    if _application is None:
        import boto3
        from botocore.config import Config
        from aws_mesouq.contracts import (
            validate_job_configuration,
            validate_result,
        )

        _application = Application(
            DynamoStore(boto3.client("dynamodb"), os.environ["STATE_TABLE"]),
            S3Objects(
                boto3.client(
                    "s3",
                    endpoint_url=f"https://s3.{os.environ['AWS_REGION']}.amazonaws.com",
                    config=Config(
                        signature_version="s3v4",
                        s3={"addressing_style": "virtual"},
                        connect_timeout=2,
                        read_timeout=3,
                        retries={"mode": "standard", "total_max_attempts": 1},
                    ),
                ),
                os.environ["DATA_BUCKET"],
            ),
            validate_configuration=validate_job_configuration,
            validate_result=validate_result,
            release_policy="v1",
            notifications=SQSNotifications(
                boto3.client("sqs"), os.environ["EVENT_QUEUE_URL"]
            ),
        )
    return _application


def site_auth():
    global _site_auth
    if _site_auth is None:
        import boto3
        from botocore.config import Config
        from .site_auth import Cognito, SiteAuth

        pem = (
            boto3.client("ssm")
            .get_parameter(
                Name=os.environ["SITE_SIGNING_KEY_PARAMETER"], WithDecryption=True
            )["Parameter"]["Value"]
            .encode()
        )

        def invited(subject):
            _, state = application().store.load()
            identity = state["identities"].get(subject)
            return bool(
                identity and not identity.get("disabled") and identity.get("projects")
            )

        _site_auth = SiteAuth(
            base_url=os.environ["SITE_BASE_URL"],
            key_id=os.environ["SITE_PUBLIC_KEY_ID"],
            private_key=pem,
            invited=invited,
            cognito=Cognito(
                os.environ["COGNITO_DOMAIN"],
                os.environ["COGNITO_ISSUER"],
                os.environ["COGNITO_CLIENT_ID"],
            ),
        )
    return _site_auth


def is_maintenance_event(event):
    """IAM authenticates EventBridge; this additionally validates its envelope.

    API Gateway adds requestContext/rawPath, so a JSON body or forged HTTP path
    cannot become a scheduled invocation. Direct Lambda callers remain IAM-only.
    """
    required = {
        "version",
        "id",
        "detail-type",
        "source",
        "account",
        "time",
        "region",
        "resources",
        "detail",
    }
    rule_arn = os.environ.get("ARCHIVE_MAINTENANCE_RULE_ARN")
    account = os.environ.get("AWS_ACCOUNT_ID")
    return (
        isinstance(event, dict)
        and set(event) == required
        and bool(rule_arn)
        and bool(account)
        and event.get("version") == "0"
        and event.get("source") == "aws.events"
        and event.get("detail-type") == "Scheduled Event"
        and event.get("resources") == [rule_arn]
        and event.get("account") == account
        and event.get("region") == os.environ.get("AWS_REGION")
        and isinstance(event.get("id"), str)
        and bool(event["id"])
        and isinstance(event.get("time"), str)
        and bool(event["time"])
        and event.get("detail") == {}
    )


def handler(event, context):
    # Only the dedicated unauthenticated Gateway route may skip token checks.
    # Return no data or CORS headers; Gateway applies its configured CORS policy.
    if (
        isinstance(event, dict)
        and event.get("routeKey") == "OPTIONS /v1/{proxy+}"
        and event.get("requestContext", {}).get("http", {}).get("method") == "OPTIONS"
        and isinstance(event.get("rawPath"), str)
        and event["rawPath"].startswith("/v1/")
    ):
        return {
            "statusCode": 204,
            "headers": {"cache-control": "no-store", "x-content-type-options": "nosniff"},
            "body": "",
        }
    if is_maintenance_event(event):
        # Keep failures as invocation errors so Lambda's asynchronous retry policy
        # can retry the durable pending tags; an HTTP503 would count as success.
        remaining_ms = getattr(context, "get_remaining_time_in_millis", None)
        return application().maintenance(remaining_ms=remaining_ms)
    try:
        if event.get("rawPath", "").startswith("/auth/"):
            return site_auth().handle(event)
        method = event.get("requestContext", {}).get("http", {}).get("method", "")
        path = event.get("rawPath", "")
        claims = None
        if not (method == "GET" and path == "/health"):
            claims = (
                event.get("requestContext", {})
                .get("authorizer", {})
                .get("jwt", {})
                .get("claims", {})
            )
            issuer, client_id = (
                os.environ["COGNITO_ISSUER"],
                os.environ["COGNITO_CLIENT_ID"],
            )
            # API Gateway validates signature and expiry; these checks additionally
            # reject ID tokens and events from an unexpected pool/application.
            if (
                claims.get("iss") != issuer
                or claims.get("client_id") != client_id
                or claims.get("token_use") != "access"
            ):
                raise APIError(
                    401, "unauthenticated", "Verified application access token required"
                )
        raw = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw, validate=True).decode("utf-8")
        if len(raw.encode()) > 200_000:
            raise APIError(413, "request_too_large", "Request too large")
        body = json.loads(
            raw,
            parse_constant=lambda _: (_ for _ in ()).throw(
                ValueError("Nonfinite number")
            ),
        )
        from .benchmark import is_benchmark_path

        if is_benchmark_path(path):
            global _benchmark_adapter
            if _benchmark_adapter is None:
                from .benchmark_http import lambda_adapter
                _benchmark_adapter = lambda_adapter(application)
            return _benchmark_adapter(event, context)
        remaining_ms = getattr(context, "get_remaining_time_in_millis", None)
        if remaining_ms is None:
            status, response = application().handle(method, path, claims, body)
        else:
            status, response = application().handle(
                method, path, claims, body, remaining_ms=remaining_ms
            )
    except APIError as exc:
        status, response = exc.status, {
            "error": {"code": exc.code, "message": exc.message}
        }
    except (ValueError, TypeError, UnicodeError):
        status, response = 400, {
            "error": {"code": "invalid_request", "message": "Invalid JSON request"}
        }
    except Exception:
        # Never expose stack traces, token claims, request bodies or storage keys.
        LOG.exception(
            "Publishing request failed request_id=%s",
            getattr(context, "aws_request_id", "unknown"),
        )
        status, response = 503, {
            "error": {
                "code": "service_unavailable",
                "message": "Service temporarily unavailable; retry idempotently",
            }
        }
    return {
        "statusCode": status,
        "headers": {
            "content-type": "application/json",
            "cache-control": "no-store",
            "x-content-type-options": "nosniff",
        },
        "body": json.dumps(response, allow_nan=False),
    }
