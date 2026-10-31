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

run "legacy_project_payload_preserved" {
  command = plan
  variables {
    projects = { fixture = { name = "Fixture" } }
  }
  assert {
    condition     = jsondecode(jsondecode(aws_dynamodb_table_item.projects["fixture"].item).payload.S) == { name = "Fixture" }
    error_message = "An unconfigured legacy project must retain exactly its original payload."
  }
  assert {
    condition     = aws_lambda_function.backend.environment[0].variables["BENCHMARK_POLICIES"] == "{}"
    error_message = "No policy may be invented for a legacy project."
  }
}
run "benchmark_operator_policy_binding" {
  command = plan
  variables {
    projects = { fixture = {
      name = "Fixture"
      benchmark_policy = {
        adapter_sha256    = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        science_revision  = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        scenario_families = ["baseline", "higher_noise", "broader_priors"]
        archive_root      = "/project/fixture/benchmark"
      }
    } }
  }
  assert {
    condition = (
      jsondecode(aws_lambda_function.backend.environment[0].variables["BENCHMARK_POLICIES"]).fixture.science_revision == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" &&
      jsondecode(jsondecode(aws_dynamodb_table_item.projects["fixture"].item).payload.S).benchmark_policy.archive_root == "/project/fixture/benchmark"
    )
    error_message = "Trusted policy bindings must reach the project record and environment fallback."
  }
}
run "invalid_benchmark_pin_rejected" {
  command = plan
  variables {
    projects = { fixture = { name = "Fixture", benchmark_policy = {
      adapter_sha256    = "bad"
      science_revision  = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
      scenario_families = ["baseline"]
      archive_root      = "/project/fixture"
    } } }
  }
  expect_failures = [var.projects]
}

run "noncanonical_archive_root_rejected" {
  command = plan
  variables {
    projects = { fixture = { name = "Fixture", benchmark_policy = {
      adapter_sha256    = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      science_revision  = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
      scenario_families = ["baseline"]
      archive_root      = "/bad path"
    } } }
  }
  expect_failures = [var.projects]
}

override_resource {
  override_during = plan
  target          = aws_cognito_user_pool_client.browser
  values          = { id = "synthetic-native-client" }
}
override_resource {
  override_during = plan
  target          = aws_cognito_user_pool_client.remembered_web
  values          = { id = "synthetic-remembered-client" }
}
override_resource {
  override_during = plan
  target          = aws_cloudfront_function.remembered_entry
  values          = { arn = "arn:aws:cloudfront::000000000000:function/synthetic-remembered-entry" }
}

run "remembered_default_off_preserves_native_client_and_private_assets" {
  command = plan
  assert {
    condition = (
      !var.remembered_browser_sessions_enabled &&
      aws_lambda_function.backend.environment[0].variables["SITE_REMEMBERED_SESSIONS_ENABLED"] == "false" &&
      aws_lambda_function.backend.environment[0].variables["COGNITO_CLIENT_ID"] == "synthetic-native-client" &&
      aws_cognito_user_pool_client.browser.refresh_token_validity == 1 &&
      aws_cognito_user_pool_client.browser.access_token_validity == 15 &&
      contains(aws_cognito_user_pool_client.browser.callback_urls, var.connector_callback_url)
    )
    error_message = "Default activation must remain off without changing the original native client or its one-day refresh policy."
  }
  assert {
    condition = (
      length(aws_cloudfront_distribution.web.default_cache_behavior[0].function_association) == 0 &&
      length(aws_cloudfront_distribution.web.default_cache_behavior[0].trusted_key_groups) > 0 &&
      aws_cloudfront_distribution.web.default_cache_behavior[0].target_origin_id == "private-web"
    )
    error_message = "Default behavior must retain private signed assets and no new active viewer association."
  }
  assert {
    condition = (
      aws_cognito_user_pool.users.mfa_configuration == "ON" &&
      !aws_cognito_user_pool_client.remembered_web.generate_secret &&
      aws_cognito_user_pool_client.remembered_web.refresh_token_validity == 30 &&
      aws_cognito_user_pool_client.remembered_web.access_token_validity == 15 &&
      aws_cognito_user_pool_client.remembered_web.callback_urls == toset(["https://synthetic.cloudfront.net/auth/callback"]) &&
      aws_cognito_user_pool_client.remembered_web.explicit_auth_flows == toset(["ALLOW_REFRESH_TOKEN_AUTH"])
    )
    error_message = "Remembered sign-in requires existing MFA, a dedicated public web-only client and bounded token lifetimes."
  }
  assert {
    condition = (
      aws_apigatewayv2_authorizer.users.jwt_configuration[0].audience == toset(["synthetic-native-client", "synthetic-remembered-client"]) &&
      aws_dynamodb_table.state.ttl[0].enabled &&
      aws_dynamodb_table.state.ttl[0].attribute_name == "auth_expires_at"
    )
    error_message = "Both exact client audiences and auth-only TTL cleanup must be configured."
  }
}

run "remembered_activation_adds_only_the_reviewed_viewer_entry_association" {
  command = plan
  variables {
    remembered_browser_sessions_enabled = true
  }
  assert {
    condition = (
      aws_lambda_function.backend.environment[0].variables["SITE_REMEMBERED_SESSIONS_ENABLED"] == "true" &&
      length(aws_cloudfront_distribution.web.default_cache_behavior[0].function_association) == 1 &&
      one(aws_cloudfront_distribution.web.default_cache_behavior[0].function_association).event_type == "viewer-request" &&
      one(aws_cloudfront_distribution.web.default_cache_behavior[0].function_association).function_arn == "arn:aws:cloudfront::000000000000:function/synthetic-remembered-entry" &&
      length(aws_cloudfront_distribution.web.default_cache_behavior[0].trusted_key_groups) > 0 &&
      aws_cloudfront_distribution.web.default_cache_behavior[0].allowed_methods == toset(["GET", "HEAD"])
    )
    error_message = "Activation must add the pinned viewer hook while preserving native signed grants and private asset method rules."
  }
}

override_resource {
  override_during = plan
  target          = aws_cloudfront_function.private_entry
  values          = { arn = "arn:aws:cloudfront::000000000000:function/synthetic-private-entry-v1" }
}
override_resource {
  override_during = plan
  target          = aws_cloudfront_function.public_entry
  values          = { arn = "arn:aws:cloudfront::000000000000:function/synthetic-public-entry-v1" }
}

run "secure_entry_defaults_only_create_unassociated_identities" {
  command = plan
  assert {
    condition = (
      !var.secure_private_alias_enabled && !var.secure_public_entry_enabled &&
      aws_cloudfront_distribution.web.default_root_object == "index.html" &&
      length(aws_cloudfront_distribution.web.ordered_cache_behavior) == 1 &&
      aws_cloudfront_distribution.web.ordered_cache_behavior[0].path_pattern == "/auth/*"
    )
    error_message = "New function identities must remain unassociated until staged publication."
  }
}
run "secure_private_alias_preserves_native_s3_boundary" {
  command = plan
  variables {
    secure_private_alias_enabled = true
  }
  assert {
    condition = (
      one(aws_cloudfront_distribution.web.default_cache_behavior[0].function_association).function_arn == "arn:aws:cloudfront::000000000000:function/synthetic-private-entry-v1" &&
      length(aws_cloudfront_distribution.web.default_cache_behavior[0].trusted_key_groups) > 0 &&
      aws_cloudfront_distribution.web.default_cache_behavior[0].target_origin_id == "private-web" &&
      aws_cloudfront_distribution.web.default_cache_behavior[0].allowed_methods == toset(["GET", "HEAD"]) &&
      aws_cloudfront_distribution.web.default_root_object == "index.html" &&
      length(aws_cloudfront_distribution.web.ordered_cache_behavior) == 1
    )
    error_message = "Stage A may replace only the hook; native signed S3 protection stays enabled."
  }
}
run "secure_public_bootstrap_has_api_only_nonforwarding_fallback" {
  command = plan
  variables {
    secure_private_alias_enabled = true
    secure_public_entry_enabled  = true
  }
  assert {
    condition = (
      aws_cloudfront_distribution.web.default_root_object == "" &&
      [for item in aws_cloudfront_distribution.web.ordered_cache_behavior : item.path_pattern] == ["/", "/index.html", "/auth/*"] &&
      alltrue([for item in slice(aws_cloudfront_distribution.web.ordered_cache_behavior, 0, 2) :
        item.target_origin_id == "regional-auth" &&
        item.allowed_methods == toset(["GET", "HEAD"]) &&
        (item.origin_request_policy_id == null || item.origin_request_policy_id == "") &&
        length(coalesce(item.trusted_key_groups, toset([]))) == 0 &&
        one(item.function_association).function_arn == "arn:aws:cloudfront::000000000000:function/synthetic-public-entry-v1"
      ]) &&
      aws_cloudfront_distribution.web.ordered_cache_behavior[2].target_origin_id == "regional-auth" &&
      aws_cloudfront_distribution.web.ordered_cache_behavior[2].allowed_methods == toset(["GET", "HEAD", "OPTIONS", "PUT", "PATCH", "POST", "DELETE"]) &&
      aws_apigatewayv2_route.site_auth.route_key == "ANY /auth/{proxy+}" &&
      length(aws_cloudfront_distribution.web.default_cache_behavior[0].trusted_key_groups) > 0
    )
    error_message = "Only exact public bootstrap paths may use the API fallback; auth and private S3 protections remain."
  }
}
run "secure_public_requires_private_alias" {
  command = plan
  variables {
    secure_public_entry_enabled = true
  }
  expect_failures = [aws_cloudfront_distribution.web]
}
