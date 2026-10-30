data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
locals {
  tags = { Project = "AWS-MesoUQ", Component = "aws-publishing" }
}
resource "aws_ecr_repository" "backend" {
  name                 = "${var.name}-backend"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false
  image_scanning_configuration { scan_on_push = true }
  tags = local.tags
}
resource "aws_ecr_lifecycle_policy" "backend" {
  repository = aws_ecr_repository.backend.name
  policy     = jsonencode({ rules = [{ rulePriority = 1, description = "Keep three untagged build images", selection = { tagStatus = "untagged", countType = "imageCountMoreThan", countNumber = 3 }, action = { type = "expire" } }] })
}
resource "aws_dynamodb_table" "state" {
  name         = "${var.name}-state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"
  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }
  server_side_encryption { enabled = true }
  ttl {
    attribute_name = "auth_expires_at"
    enabled        = true
  }
  deletion_protection_enabled = true
  tags                        = local.tags
}
resource "aws_dynamodb_table_item" "projects" {
  for_each   = var.projects
  table_name = aws_dynamodb_table.state.name
  hash_key   = "pk"
  range_key  = "sk"
  item       = jsonencode({ pk = { S = "STATE" }, sk = { S = "projects:${each.key}" }, payload = { S = jsonencode(merge({ name = each.value.name }, each.value.benchmark_policy == null ? {} : { benchmark_policy = each.value.benchmark_policy })) } })
}
resource "aws_dynamodb_table_item" "memberships" {
  for_each   = toset(nonsensitive(keys(var.memberships)))
  table_name = aws_dynamodb_table.state.name
  hash_key   = "pk"
  range_key  = "sk"
  item       = jsonencode({ pk = { S = "STATE" }, sk = { S = "identities:${each.key}" }, payload = { S = jsonencode(var.memberships[each.key]) } })
}
resource "aws_dynamodb_table_item" "models" {
  for_each   = var.trusted_models
  table_name = aws_dynamodb_table.state.name
  hash_key   = "pk"
  range_key  = "sk"
  item       = jsonencode({ pk = { S = "STATE" }, sk = { S = "models:${each.value.project_id}:${each.value.version}" }, payload = { S = jsonencode(merge({ for key, value in each.value : key => value if key != "provenance_json" }, { trusted = true, validation = { passed = true } }, each.value.provenance_json == null ? {} : { provenance = jsondecode(each.value.provenance_json) })) } })
}
resource "aws_s3_bucket" "data" {
  bucket_prefix = "${var.name}-data-"
  force_destroy = false
  tags          = local.tags
}
resource "aws_s3_bucket_public_access_block" "data" {
  bucket                  = aws_s3_bucket.data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  bucket = aws_s3_bucket.data.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}
resource "aws_s3_bucket_policy" "data" {
  bucket = aws_s3_bucket.data.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Sid = "TLSOnly", Effect = "Deny", Principal = "*", Action = "s3:*", Resource = [aws_s3_bucket.data.arn, "${aws_s3_bucket.data.arn}/*"], Condition = { Bool = { "aws:SecureTransport" = "false" } } },
    # Uploads cannot attach the lifecycle eligibility tag inline. Only the
    # private server's separate, clock-gated PutObjectTagging flow can add it.
    { Sid = "NoInlineArchiveEligibility", Effect = "Deny", Principal = "*", Action = "s3:PutObject", Resource = "${aws_s3_bucket.data.arn}/*", Condition = { Null = { "s3:RequestObjectTag/archive_verified" = "false" } } }
  ] })
}
resource "aws_s3_bucket_lifecycle_configuration" "data" {
  bucket = aws_s3_bucket.data.id
  rule {
    id     = "expire-only-after-verified-karolina-archive"
    status = "Enabled"
    filter {
      tag {
        key   = "archive_verified"
        value = "true"
      }
    }
    expiration { days = 30 }
  }
  rule {
    id     = "abort-incomplete-multipart-uploads"
    status = "Enabled"
    filter { prefix = "" }
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
  }
}
resource "aws_s3_bucket_cors_configuration" "data" {
  bucket = aws_s3_bucket.data.id
  cors_rule {
    allowed_methods = ["GET", "PUT", "HEAD"]
    allowed_origins = ["https://${aws_cloudfront_distribution.web.domain_name}"]
    allowed_headers = ["content-type", "x-amz-checksum-sha256", "if-none-match"]
    expose_headers  = ["ETag", "x-amz-checksum-sha256"]
    max_age_seconds = 300
  }
}
resource "aws_sqs_queue" "dead" {
  name                      = "${var.name}-events-dead"
  message_retention_seconds = 345600
  sqs_managed_sse_enabled   = true
  tags                      = local.tags
}
resource "aws_sqs_queue" "events" {
  name                       = "${var.name}-events"
  message_retention_seconds  = 86400
  visibility_timeout_seconds = 60
  sqs_managed_sse_enabled    = true
  redrive_policy             = jsonencode({ deadLetterTargetArn = aws_sqs_queue.dead.arn, maxReceiveCount = 3 })
  tags                       = local.tags
}
resource "aws_cognito_user_pool" "users" {
  name                     = var.name
  mfa_configuration        = "ON"
  user_pool_tier           = "LITE"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  deletion_protection      = "ACTIVE"
  admin_create_user_config { allow_admin_create_user_only = true }
  software_token_mfa_configuration { enabled = true }
  password_policy {
    minimum_length                   = 14
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 3
  }
  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }
  tags = local.tags
}
resource "aws_cognito_user_pool_domain" "users" {
  domain                = "${var.cognito_domain_prefix}-${substr(sha256("${data.aws_caller_identity.current.account_id}:${var.region}:${var.name}"), 0, 12)}"
  user_pool_id          = aws_cognito_user_pool.users.id
  managed_login_version = 1
}
resource "aws_cognito_user_pool_client" "browser" {
  name                                 = "${var.name}-browser"
  user_pool_id                         = aws_cognito_user_pool.users.id
  generate_secret                      = false
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = ["https://${aws_cloudfront_distribution.web.domain_name}/auth/callback", var.connector_callback_url]
  logout_urls                          = ["https://${aws_cloudfront_distribution.web.domain_name}/auth/login"]
  prevent_user_existence_errors        = "ENABLED"
  enable_token_revocation              = true
  access_token_validity                = 15
  id_token_validity                    = 15
  refresh_token_validity               = 1
  explicit_auth_flows                  = ["ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }
}
# Separate public PKCE client; native connector/legacy web client stays unchanged.
resource "aws_cognito_user_pool_client" "remembered_web" {
  name                                 = "${var.name}-remembered-web"
  user_pool_id                         = aws_cognito_user_pool.users.id
  generate_secret                      = false
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = ["https://${aws_cloudfront_distribution.web.domain_name}/auth/callback"]
  logout_urls                          = ["https://${aws_cloudfront_distribution.web.domain_name}/auth/login"]
  prevent_user_existence_errors        = "ENABLED"
  enable_token_revocation              = true
  access_token_validity                = 15
  id_token_validity                    = 15
  refresh_token_validity               = 30
  explicit_auth_flows                  = ["ALLOW_REFRESH_TOKEN_AUTH"]
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }
}
resource "aws_cloudwatch_log_group" "backend" {
  name              = "/aws/lambda/${var.name}-backend"
  retention_in_days = 7
  tags              = local.tags
}
resource "aws_iam_role" "backend" {
  name               = "${var.name}-backend"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }] })
}
resource "aws_iam_role_policy" "backend" {
  role = aws_iam_role.backend.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.backend.arn}:*" },
    { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:TransactWriteItems", "dynamodb:PutItem"], Resource = aws_dynamodb_table.state.arn },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:GetObjectTagging", "s3:PutObjectTagging", "s3:DeleteObject"], Resource = "${aws_s3_bucket.data.arn}/projects/*/artifacts/*" },
    # HeadObject must distinguish absent (404) from denied (403) before releasing
    # archived-object upload capacity. This bucket is private to the control plane.
    { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.data.arn },
    { Effect = "Allow", Action = ["sqs:SendMessage"], Resource = aws_sqs_queue.events.arn },
    { Effect = "Allow", Action = ["ssm:GetParameter"], Resource = "arn:${data.aws_partition.current.partition}:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${var.site_signing_key_parameter_name}" }
  ] })
}
resource "aws_lambda_function" "backend" {
  function_name = "${var.name}-backend"
  role          = aws_iam_role.backend.arn
  package_type  = "Image"
  image_uri     = var.image_uri
  memory_size   = 256
  timeout       = 28
  # Use the account's unreserved pool. New Free accounts can allow only ten
  # concurrent executions and cannot satisfy AWS's minimum unreserved pool required
  # to allocate reserved concurrency. API throttles and job admission still apply.
  environment {
    variables = {
      BENCHMARK_POLICIES               = jsonencode({ for id, project in var.projects : id => project.benchmark_policy if project.benchmark_policy != null })
      STATE_TABLE                      = aws_dynamodb_table.state.name
      DATA_BUCKET                      = aws_s3_bucket.data.id
      EVENT_QUEUE_URL                  = aws_sqs_queue.events.url
      COGNITO_ISSUER                   = "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.users.id}"
      COGNITO_CLIENT_ID                = aws_cognito_user_pool_client.browser.id
      COGNITO_REMEMBERED_CLIENT_ID     = aws_cognito_user_pool_client.remembered_web.id
      SITE_REMEMBERED_SESSIONS_ENABLED = tostring(var.remembered_browser_sessions_enabled)
      ARCHIVE_MAINTENANCE_RULE_ARN     = aws_cloudwatch_event_rule.archive_maintenance.arn
      AWS_ACCOUNT_ID                   = data.aws_caller_identity.current.account_id
      SITE_BASE_URL                    = "https://${aws_cloudfront_distribution.web.domain_name}"
      SITE_PUBLIC_KEY_ID               = aws_cloudfront_public_key.site.id
      SITE_SIGNING_KEY_PARAMETER       = var.site_signing_key_parameter_name
      COGNITO_DOMAIN                   = "https://${aws_cognito_user_pool_domain.users.domain}.auth.${var.region}.amazoncognito.com"
    }
  }
  depends_on = [aws_cloudwatch_log_group.backend, aws_iam_role_policy.backend]
  tags       = local.tags
}
resource "aws_apigatewayv2_api" "api" {
  name          = var.name
  protocol_type = "HTTP"
  cors_configuration {
    allow_origins = ["https://${aws_cloudfront_distribution.web.domain_name}"]
    allow_methods = ["GET", "POST", "OPTIONS"]
    allow_headers = ["authorization", "content-type"]
    max_age       = 300
  }
  tags = local.tags
}
resource "aws_apigatewayv2_authorizer" "users" {
  api_id           = aws_apigatewayv2_api.api.id
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]
  name             = "cognito-access"
  jwt_configuration {
    audience = [aws_cognito_user_pool_client.browser.id, aws_cognito_user_pool_client.remembered_web.id]
    issuer   = "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.users.id}"
  }
}
resource "aws_apigatewayv2_integration" "backend" {
  api_id                 = aws_apigatewayv2_api.api.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.backend.invoke_arn
  payload_format_version = "2.0"
}
resource "aws_apigatewayv2_route" "health" {
  api_id    = aws_apigatewayv2_api.api.id
  route_key = "GET /health"
  target    = "integrations/${aws_apigatewayv2_integration.backend.id}"
}
resource "aws_apigatewayv2_route" "api" {
  api_id               = aws_apigatewayv2_api.api.id
  route_key            = "ANY /v1/{proxy+}"
  target               = "integrations/${aws_apigatewayv2_integration.backend.id}"
  authorization_type   = "JWT"
  authorizer_id        = aws_apigatewayv2_authorizer.users.id
  authorization_scopes = ["openid"]
}
# Browsers send preflights without bearer tokens. This route takes priority over
# ANY; Lambda returns an empty 204 and Gateway applies its configured CORS policy.
resource "aws_apigatewayv2_route" "preflight" {
  api_id             = aws_apigatewayv2_api.api.id
  route_key          = "OPTIONS /v1/{proxy+}"
  target             = "integrations/${aws_apigatewayv2_integration.backend.id}"
  authorization_type = "NONE"
}
resource "aws_apigatewayv2_stage" "api" {
  api_id      = aws_apigatewayv2_api.api.id
  name        = "$default"
  auto_deploy = true
  default_route_settings {
    throttling_burst_limit = 10
    throttling_rate_limit  = 5
  }
}
resource "aws_lambda_permission" "api" {
  statement_id  = "APIGatewayOnly"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.backend.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.api.execution_arn}/*/*"
}
resource "aws_budgets_budget" "near_zero" {
  name         = "${var.name}-near-zero"
  budget_type  = "COST"
  limit_amount = var.budget_usd
  limit_unit   = "USD"
  time_unit    = "MONTHLY"
}

# API-created clients require an explicit hosted-UI branding configuration.
# Classic hosted UI keeps this on Cognito Lite without paid managed-login features.
resource "aws_cognito_user_pool_ui_customization" "classic" {
  user_pool_id = aws_cognito_user_pool.users.id
  client_id    = aws_cognito_user_pool_client.browser.id
  css          = ".label-customizable { font-weight: 400; }"
  depends_on   = [aws_cognito_user_pool_domain.users]
}


resource "aws_cloudwatch_event_rule" "archive_maintenance" {
  name                = "${var.name}-archive-maintenance"
  description         = "Reconcile expiry tags only thirty days after durable verified archive receipts"
  schedule_expression = "rate(1 day)"
  tags                = local.tags
}
resource "aws_cloudwatch_event_target" "archive_maintenance" {
  rule       = aws_cloudwatch_event_rule.archive_maintenance.name
  target_id  = "archive-retention"
  arn        = aws_lambda_function.backend.arn
  depends_on = [aws_lambda_permission.archive_maintenance, aws_lambda_function_event_invoke_config.backend]
  retry_policy {
    maximum_event_age_in_seconds = 86400
    maximum_retry_attempts       = 2
  }
}
resource "aws_lambda_permission" "archive_maintenance" {
  statement_id   = "ArchiveMaintenanceScheduleOnly"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.backend.function_name
  principal      = "events.amazonaws.com"
  source_arn     = aws_cloudwatch_event_rule.archive_maintenance.arn
  source_account = data.aws_caller_identity.current.account_id
}
resource "aws_lambda_function_event_invoke_config" "backend" {
  function_name                = aws_lambda_function.backend.function_name
  maximum_event_age_in_seconds = 3600
  maximum_retry_attempts       = 2
}
