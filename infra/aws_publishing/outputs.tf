output "api_url" { value = aws_apigatewayv2_api.api.api_endpoint }
output "website_url" { value = "https://${aws_cloudfront_distribution.web.domain_name}/auth/login" }
output "portal_origin" { value = "https://${aws_cloudfront_distribution.web.domain_name}" }
output "data_bucket" { value = aws_s3_bucket.data.id }
output "website_bucket" { value = aws_s3_bucket.web.id }
output "ecr_repository" { value = aws_ecr_repository.backend.repository_url }
output "github_publish_role_arn" { value = aws_iam_role.github.arn }
output "cognito_user_pool_id" { value = aws_cognito_user_pool.users.id }
# Native connector/legacy clients keep this original one-day-refresh client ID.
output "cognito_client_id" { value = aws_cognito_user_pool_client.browser.id }
output "cognito_remembered_web_client_id" { value = aws_cognito_user_pool_client.remembered_web.id }
output "cognito_domain" { value = "https://${aws_cognito_user_pool_domain.users.domain}.auth.${var.region}.amazoncognito.com" }
output "state_table" { value = aws_dynamodb_table.state.name }
