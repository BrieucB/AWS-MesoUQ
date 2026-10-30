resource "aws_s3_bucket" "web" {
  bucket_prefix = "${var.name}-web-"
  force_destroy = false
  tags          = local.tags
}
resource "aws_s3_bucket_public_access_block" "web" {
  bucket                  = aws_s3_bucket.web.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_server_side_encryption_configuration" "web" {
  bucket = aws_s3_bucket.web.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}
resource "aws_cloudfront_origin_access_control" "web" {
  name                              = "${var.name}-web"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}
resource "aws_cloudfront_response_headers_policy" "web" {
  name = "${var.name}-security"
  custom_headers_config {
    items {
      header   = "Cache-Control"
      value    = "no-store"
      override = true
    }
  }
  security_headers_config {
    content_type_options { override = true }
    frame_options {
      frame_option = "DENY"
      override     = true
    }
    referrer_policy {
      referrer_policy = "no-referrer"
      override        = true
    }
    strict_transport_security {
      access_control_max_age_sec = 31536000
      include_subdomains         = true
      override                   = true
    }
    content_security_policy {
      content_security_policy = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self' https://*.execute-api.${var.region}.amazonaws.com https://*.s3.${var.region}.amazonaws.com https://*.auth.${var.region}.amazoncognito.com; frame-ancestors 'none'; base-uri 'self'; object-src 'none'"
      override                = true
    }
  }
}
# No credential parsing or origin access: only missing-grant root entry routing.
resource "aws_cloudfront_function" "remembered_entry" {
  name    = "${var.name}-remembered-entry"
  runtime = "cloudfront-js-1.0"
  comment = "Resume remembered browser login when root entry has no signed grants"
  publish = true
  code    = file("${path.module}/functions/remembered-entry.js")
}
resource "aws_cloudfront_distribution" "web" {
  enabled             = true
  default_root_object = "index.html"
  price_class         = "PriceClass_100"
  origin {
    domain_name              = aws_s3_bucket.web.bucket_regional_domain_name
    origin_id                = "private-web"
    origin_access_control_id = aws_cloudfront_origin_access_control.web.id
  }
  origin {
    domain_name = replace(aws_apigatewayv2_api.site_auth.api_endpoint, "https://", "")
    origin_id   = "regional-auth"
    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }
  ordered_cache_behavior {
    path_pattern               = "/auth/*"
    target_origin_id           = "regional-auth"
    viewer_protocol_policy     = "https-only"
    allowed_methods            = ["GET", "HEAD", "OPTIONS", "PUT", "PATCH", "POST", "DELETE"]
    cached_methods             = ["GET", "HEAD"]
    cache_policy_id            = aws_cloudfront_cache_policy.web.id
    origin_request_policy_id   = aws_cloudfront_origin_request_policy.auth.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.web.id
  }
  default_cache_behavior {
    dynamic "function_association" {
      for_each = var.remembered_browser_sessions_enabled ? [1] : []
      content {
        event_type   = "viewer-request"
        function_arn = aws_cloudfront_function.remembered_entry.arn
      }
    }
    trusted_key_groups         = [aws_cloudfront_key_group.site.id]
    target_origin_id           = "private-web"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = aws_cloudfront_cache_policy.web.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.web.id
  }
  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }
  viewer_certificate { cloudfront_default_certificate = true }
  tags = local.tags
}
resource "aws_s3_bucket_policy" "web" {
  bucket = aws_s3_bucket.web.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Sid = "CloudFrontOnly", Effect = "Allow", Principal = { Service = "cloudfront.amazonaws.com" }, Action = "s3:GetObject", Resource = "${aws_s3_bucket.web.arn}/*", Condition = { StringEquals = { "AWS:SourceArn" = aws_cloudfront_distribution.web.arn } } },
    { Sid = "TLSOnly", Effect = "Deny", Principal = "*", Action = "s3:*", Resource = [aws_s3_bucket.web.arn, "${aws_s3_bucket.web.arn}/*"], Condition = { Bool = { "aws:SecureTransport" = "false" } } }
  ] })
}
resource "aws_iam_role" "github" {
  name               = "${var.name}-github-publish"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Federated = var.github_oidc_provider_arn }, Action = "sts:AssumeRoleWithWebIdentity", Condition = { StringEquals = { "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com", "token.actions.githubusercontent.com:sub" = "${var.github_oidc_subject_prefix}:environment:${var.github_environment}" } } }] })
}
resource "aws_iam_role_policy" "github" {
  role = aws_iam_role.github.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*" },
    { Effect = "Allow", Action = ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:DescribeRepositories"], Resource = aws_ecr_repository.backend.arn },
    { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.web.arn },
    { Effect = "Allow", Action = ["s3:PutObject", "s3:GetObject"], Resource = "${aws_s3_bucket.web.arn}/*" },
    { Effect = "Allow", Action = ["lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:UpdateFunctionCode", "lambda:UpdateFunctionConfiguration"], Resource = aws_lambda_function.backend.arn },
    { Effect = "Allow", Action = ["cloudfront:CreateInvalidation"], Resource = aws_cloudfront_distribution.web.arn }
  ] })
}

resource "aws_cloudfront_cache_policy" "web" {
  name        = "${var.name}-no-cache"
  min_ttl     = 0
  default_ttl = 0
  max_ttl     = 0
  parameters_in_cache_key_and_forwarded_to_origin {
    cookies_config { cookie_behavior = "none" }
    headers_config { header_behavior = "none" }
    query_strings_config { query_string_behavior = "none" }
  }
}

# Only public key material is present at the edge or in Terraform state.
resource "aws_cloudfront_public_key" "site" {
  name        = "${var.name}-site"
  encoded_key = var.site_signing_public_key_pem
}
resource "aws_cloudfront_key_group" "site" {
  name  = "${var.name}-site"
  items = [aws_cloudfront_public_key.site.id]
}
resource "aws_cloudfront_origin_request_policy" "auth" {
  name = "${var.name}-auth"
  cookies_config { cookie_behavior = "all" }
  headers_config {
    header_behavior = "whitelist"
    headers { items = ["Origin"] }
  }
  query_strings_config { query_string_behavior = "all" }
}
# Separate bootstrap API prevents a CloudFront/CORS/Cognito dependency cycle.
# No CORS: token hydration is strictly same-origin through CloudFront.
resource "aws_apigatewayv2_api" "site_auth" {
  name          = "${var.name}-site-auth"
  protocol_type = "HTTP"
  tags          = local.tags
}
resource "aws_apigatewayv2_integration" "site_auth" {
  api_id                 = aws_apigatewayv2_api.site_auth.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.backend.invoke_arn
  payload_format_version = "2.0"
}
resource "aws_apigatewayv2_route" "site_auth" {
  api_id    = aws_apigatewayv2_api.site_auth.id
  route_key = "ANY /auth/{proxy+}"
  target    = "integrations/${aws_apigatewayv2_integration.site_auth.id}"
}
resource "aws_apigatewayv2_stage" "site_auth" {
  api_id      = aws_apigatewayv2_api.site_auth.id
  name        = "$default"
  auto_deploy = true
  default_route_settings {
    throttling_burst_limit = 5
    throttling_rate_limit  = 2
  }
}
resource "aws_lambda_permission" "site_auth" {
  statement_id  = "SiteAuthGatewayOnly"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.backend.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.site_auth.execution_arn}/*/*/auth/*"
}
