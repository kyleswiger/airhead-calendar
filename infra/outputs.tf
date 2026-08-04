output "site_url" {
  description = "Public URL for the kitchen display / PWA."
  value       = "https://${local.fqdn}"
}

output "site_bucket" {
  description = "S3 bucket the frontend build is synced into."
  value       = aws_s3_bucket.site.id
}

output "cloudfront_distribution_id" {
  description = "Needed for cache invalidation after a deploy."
  value       = aws_cloudfront_distribution.site.id
}

output "cloudfront_domain_name" {
  description = "Origin CloudFront hostname, works before DNS propagates."
  value       = aws_cloudfront_distribution.site.domain_name
}

# --- M1: control plane -------------------------------------------------------

# Read off the API rather than the stage: the $default stage's invoke_url carries a
# trailing slash, and the frontend joins paths onto this, so the stage value yields
# `//events`. Same host, no slash.
output "api_base_url" {
  description = "Base URL for the control plane API. The frontend needs this as VITE_API_BASE at build time."
  value       = aws_apigatewayv2_api.api.api_endpoint
}

output "dynamodb_table_name" {
  description = "Single table holding every household entity."
  value       = aws_dynamodb_table.airhead.name
}

output "api_function_name" {
  description = "api Lambda name, for `aws logs tail` and out-of-band code updates."
  value       = aws_lambda_function.api.function_name
}
