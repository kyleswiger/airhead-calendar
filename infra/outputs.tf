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
