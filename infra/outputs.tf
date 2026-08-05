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

# --- M2: agent ---------------------------------------------------------------

output "agent_function_name" {
  description = "agent Lambda name, for `aws logs tail` and out-of-band code updates."
  value       = aws_lambda_function.agent.function_name
}

# The name, not the value. There is no output for the key itself and there must not
# be: an output is stored in state and printed by `terraform output` with no redaction
# unless marked sensitive, and `sensitive` only hides it from the console.
output "anthropic_api_key_parameter_name" {
  description = "SSM parameter the agent reads its API key from. Terraform creates it with a placeholder; populate it with `aws ssm put-parameter` - see secrets.tf."
  value       = aws_ssm_parameter.anthropic_api_key.name
}

# Needed by the put-parameter call that populates the parameter above: `--overwrite`
# without an explicit `--key-id` is the documented way to end up with a value sitting
# under a different key than the one the agent's kms:Decrypt grant names, which fails
# at the first turn with an AccessDenied that points at KMS rather than at the typo.
output "secrets_kms_key_id" {
  description = "CMK encrypting the SecureString parameters. Pass to `aws ssm put-parameter --key-id`."
  value       = aws_kms_key.secrets.key_id
}
