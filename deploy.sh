#!/usr/bin/env bash
# Build the frontend, sync it to S3, and invalidate CloudFront.
#
# Infrastructure changes go through `terraform apply` in infra/ separately -
# this script only ships the site.
set -euo pipefail

cd "$(dirname "$0")"

echo "==> terraform outputs"
BUCKET=$(terraform -chdir=infra output -raw site_bucket)
DIST_ID=$(terraform -chdir=infra output -raw cloudfront_distribution_id)
SITE_URL=$(terraform -chdir=infra output -raw site_url)

echo "==> building frontend"
# npm ci, not npm install: a stale node_modules beside an updated package.json
# fails at build time with an unresolvable-import error that looks like broken
# source. Cost an afternoon on the cabin deploy; not repeating it.
(cd frontend && npm ci && npm run build)

echo "==> syncing to s3://${BUCKET}"
# Hashed assets are immutable and cache hard. index.html must not, or the
# kitchen display keeps serving a stale bundle after every deploy.
aws s3 sync frontend/dist "s3://${BUCKET}" \
  --delete \
  --exclude "index.html" \
  --cache-control "public,max-age=31536000,immutable"

aws s3 cp frontend/dist/index.html "s3://${BUCKET}/index.html" \
  --cache-control "no-cache,must-revalidate"

echo "==> invalidating CloudFront ${DIST_ID}"
aws cloudfront create-invalidation \
  --distribution-id "${DIST_ID}" \
  --paths "/index.html" "/" \
  --query 'Invalidation.Id' --output text

echo "==> done: ${SITE_URL}"
