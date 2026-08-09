# Keyless CI/CD: .github/workflows/deploy.yml assumes this role via GitHub's
# OIDC provider (sts:AssumeRoleWithWebIdentity), so no long-lived AWS access
# keys are stored as repo secrets. The only secret the repo holds is the role
# ARN itself:
#
#   terraform output -raw github_actions_role_arn \
#     | gh secret set AWS_GITHUB_ACTIONS_ROLE_ARN -R kyleswiger/airhead-calendar
#
# The module lives in the shared tooling repo. ref=main because the tooling
# repo publishes no tags yet; when it does, pin one so a push there cannot
# change this stack's IAM without a deliberate ref bump here.
module "github_actions" {
  source = "git::https://github.com/kyleswiger/aws-deployment-tooling.git//terraform-modules/github-oidc-role?ref=main"

  name_prefix = var.project
  github_repo = "kyleswiger/airhead-calendar"

  # An account may hold exactly one OIDC provider per issuer URL. If another
  # stack in this account already created token.actions.githubusercontent.com,
  # set this to false in terraform.tfvars and the module looks it up instead.
  create_oidc_provider = var.create_github_oidc_provider

  # Deploys run only on pushes to main (deploy.yml has no pull_request
  # trigger), so the trust policy does not include the module's default
  # `pull_request` claim: a PR from this repo cannot assume the role at all.
  # PR-time CI (fmt/validate/tests) needs no AWS credentials by design.
  # Both subject formats. This repo's tokens carry GitHub's immutable sub
  # claim with account/repo IDs spliced into the repo segment — verified
  # empirically, not from docs: CloudTrail's AccessDenied events for
  # AssumeRoleWithWebIdentity record the token's actual subject as
  # userIdentity.userName =
  #   repo:kyleswiger@5436172/airhead-calendar@1319624799:ref:refs/heads/main
  # (`gh api .../actions/oidc/customization/sub` reports the same value as
  # sub_claim_prefix). The plain form stays as a fallback should the
  # customization ever revert — older repos (e.g. sportscard-intelligence)
  # still emit it. The IDs are immutable, so a rename or a squatted
  # replacement repo cannot satisfy the first claim.
  subject_claims = [
    "repo:kyleswiger@5436172/airhead-calendar@1319624799:ref:refs/heads/main",
    "repo:kyleswiger/airhead-calendar:ref:refs/heads/main",
  ]

  # Least privilege for exactly what deploy.yml does, and nothing else: no
  # iam:*, no lambda:UpdateFunctionConfiguration (env vars stay Terraform's),
  # no s3 access outside the site bucket. The two read-only discovery
  # statements exist because this public repo commits no real deployment
  # values (account id, distribution id, API endpoint) - the workflow
  # resolves them at run time instead of reading them from the tree.
  policy_statements = [
    {
      sid = "UpdateLambdaCode"
      actions = [
        "lambda:UpdateFunctionCode",
        "lambda:GetFunction",
      ]
      resources = [
        aws_lambda_function.api.arn,
        aws_lambda_function.agent.arn,
      ]
    },
    {
      sid = "SyncSiteBucket"
      actions = [
        "s3:ListBucket",
      ]
      resources = [aws_s3_bucket.site.arn]
    },
    {
      sid = "WriteSiteObjects"
      actions = [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
      ]
      resources = ["${aws_s3_bucket.site.arn}/*"]
    },
    {
      sid = "InvalidateSite"
      actions = [
        "cloudfront:CreateInvalidation",
      ]
      resources = [aws_cloudfront_distribution.site.arn]
    },
    {
      # ListDistributions supports no resource-level scoping; the workflow
      # uses it once, to find the distribution id by its comment.
      sid = "DiscoverDistribution"
      actions = [
        "cloudfront:ListDistributions",
      ]
      resources = ["*"]
    },
    {
      # GET /apis so the frontend build can resolve VITE_API_BASE from the
      # live HTTP API instead of a committed endpoint. Read-only.
      sid = "DiscoverApiEndpoint"
      actions = [
        "apigateway:GET",
      ]
      resources = ["arn:aws:apigateway:${var.region}::/apis"]
    },
  ]

  tags = {
    Project = var.project
  }
}

variable "create_github_oidc_provider" {
  description = "Create the GitHub OIDC provider in this account. Set false if another stack already owns token.actions.githubusercontent.com - a second provider for the same issuer fails with EntityAlreadyExists."
  type        = bool
  default     = true
}

output "github_actions_role_arn" {
  description = "Role deploy.yml assumes via OIDC. Store as the AWS_GITHUB_ACTIONS_ROLE_ARN repo secret."
  value       = module.github_actions.role_arn
}
