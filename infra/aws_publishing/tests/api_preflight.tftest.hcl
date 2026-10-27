# All provider operations are mocked, and this run only plans. No AWS account,
# credentials, real signing key, backend state or research fixtures are used.
mock_provider "aws" {
  override_during = plan

  mock_data "aws_caller_identity" {
    defaults = { account_id = "000000000000" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
}

# Distinct IDs make an accidental connection to the public auth API observable.
override_resource {
  override_during = plan
  target          = aws_apigatewayv2_api.api
  values = {
    id            = "synthetic-control-api"
    execution_arn = "arn:aws:execute-api:eu-central-1:000000000000:synthetic-control-api"
  }
}
override_resource {
  override_during = plan
  target          = aws_apigatewayv2_api.site_auth
  values = {
    id            = "synthetic-auth-api"
    execution_arn = "arn:aws:execute-api:eu-central-1:000000000000:synthetic-auth-api"
  }
}
override_resource {
  override_during = plan
  target          = aws_apigatewayv2_integration.backend
  values          = { id = "synthetic-control-integration" }
}
override_resource {
  override_during = plan
  target          = aws_apigatewayv2_integration.site_auth
  values          = { id = "synthetic-auth-integration" }
}
override_resource {
  override_during = plan
  target          = aws_cloudfront_distribution.web
  values          = { domain_name = "synthetic.cloudfront.net" }
}
override_resource {
  override_during = plan
  target          = aws_apigatewayv2_authorizer.users
  values          = { id = "synthetic-cognito-authorizer" }
}

variables {
  github_oidc_subject_prefix      = "repo:Example/Fixture"
  github_oidc_provider_arn        = "arn:aws:iam::000000000000:oidc-provider/token.actions.githubusercontent.com"
  image_uri                       = "000000000000.dkr.ecr.eu-central-1.amazonaws.com/synthetic@sha256:0000000000000000000000000000000000000000000000000000000000000000"
  site_signing_public_key_pem     = "-----BEGIN PUBLIC KEY-----\nSYNTHETIC-NOT-A-KEY\n-----END PUBLIC KEY-----"
  site_signing_key_parameter_name = "/synthetic/site-signing-key"
}

run "preflight_preserves_authenticated_api_boundary" {
  command = plan

  assert {
    condition = (
      aws_apigatewayv2_route.preflight.route_key == "OPTIONS /v1/{proxy+}" &&
      aws_apigatewayv2_route.preflight.authorization_type == "NONE" &&
      (aws_apigatewayv2_route.preflight.authorizer_id == null || aws_apigatewayv2_route.preflight.authorizer_id == "") &&
      try(length(aws_apigatewayv2_route.preflight.authorization_scopes), 0) == 0
    )
    error_message = "Only OPTIONS below /v1 may bypass authorization; preflight must not require a token or scope."
  }

  assert {
    condition = (
      aws_apigatewayv2_route.preflight.api_id == aws_apigatewayv2_api.api.id &&
      aws_apigatewayv2_route.preflight.target == "integrations/${aws_apigatewayv2_integration.backend.id}"
    )
    error_message = "Preflight must use the protected control API and its existing backend integration."
  }

  assert {
    condition = (
      aws_apigatewayv2_route.api.route_key == "ANY /v1/{proxy+}" &&
      aws_apigatewayv2_route.api.api_id == aws_apigatewayv2_api.api.id &&
      aws_apigatewayv2_route.api.target == "integrations/${aws_apigatewayv2_integration.backend.id}" &&
      aws_apigatewayv2_route.api.authorization_type == "JWT" &&
      aws_apigatewayv2_route.api.authorizer_id == aws_apigatewayv2_authorizer.users.id &&
      aws_apigatewayv2_route.api.authorization_scopes == toset(["openid"])
    )
    error_message = "Actual API methods must retain their Cognito JWT authorizer and openid scope."
  }

  assert {
    condition = (
      aws_apigatewayv2_api.api.cors_configuration[0].allow_origins == toset(["https://synthetic.cloudfront.net"]) &&
      aws_apigatewayv2_api.api.cors_configuration[0].allow_headers == toset(["authorization", "content-type"]) &&
      aws_apigatewayv2_api.api.cors_configuration[0].allow_methods == toset(["GET", "POST", "OPTIONS"]) &&
      !coalesce(aws_apigatewayv2_api.api.cors_configuration[0].allow_credentials, false)
    )
    error_message = "CORS must remain limited to the configured portal origin, allowed headers/methods and token-only requests."
  }
}
