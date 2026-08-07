# Secrets. Not in agent.tf, and not in iam.tf: the parameters here outlive the
# function that reads them, and M3's Google OAuth refresh tokens land beside the
# Anthropic key rather than inside a per-milestone file. The IAM that grants access
# to each one stays in iam.tf, on the role that uses it.
#
# PRD §13: every secret is an SSM Parameter Store SecureString. No AWS secrets in
# the repo, ever - and this repo is public.

locals {
  # Structural, not configurable. The second path segment is the *owning role*, and
  # that is the whole design: the agent's key lives under /<project>/agent/, M3's
  # refresh tokens will live under /<project>/sync/, and each role's policy names a
  # parameter ARN outright. See the trap note on the agent's SSM policy in iam.tf -
  # a path-wildcard grant here would quietly undo PRD §13's role separation the day
  # the sync parameters appear.
  anthropic_api_key_parameter = "/${var.project}/agent/anthropic-api-key"
}

# A customer-managed key rather than the account's `aws/ssm` key, for two reasons
# that both bite at the IAM layer:
#
#   1. A KMS IAM statement's Resource must be a key ARN - aliases are not accepted -
#      so scoping `kms:Decrypt` to `aws/ssm` means looking its key id up with a data
#      source. That key does not exist until the account's first SecureString, so on
#      a fresh account the lookup fails on the very first plan, before the parameter
#      that would create it. A key declared here has no such ordering problem.
#   2. The key policy is a second, independent gate. Even a role that somehow gained
#      `ssm:GetParameter` cannot read the value without also being allowed by this.
#
# Cost: ~$1/month, plus usage that rounds to nothing at a few decrypts per turn. That
# is a real addition to the PRD §16 table and the only non-token line M2 adds. It buys
# the separation §13 asks for; the free alternative buys a data-source ordering trap.
#
# No explicit key policy. Omitting it gets the AWS default - root of this account may
# administer the key - and every actual grant is made in IAM, on the role, next to the
# ssm:GetParameter it pairs with. Writing a custom policy here is the standard way to
# lock yourself out of a key you cannot then delete for 30 days.
resource "aws_kms_key" "secrets" {
  description = "Encrypts ${var.project} SSM SecureString parameters (model credentials, OAuth tokens)."

  # Annual rotation of the backing key material. Free, invisible to callers - old
  # ciphertext stays readable - and it means a key that lives for years is not a
  # single piece of material that has protected every secret since M2.
  enable_key_rotation = true

  # The maximum. Deleting a KMS key is irreversible and takes every ciphertext with
  # it; 30 days is the longest window in which to notice and cancel.
  deletion_window_in_days = 30
}

resource "aws_kms_alias" "secrets" {
  name          = "alias/${var.project}-secrets"
  target_key_id = aws_kms_key.secrets.key_id
}

# THE VALUE IS A PLACEHOLDER AND IS MEANT TO STAY ONE. Terraform creates the
# parameter; a human puts the key in it, once, out of band:
#
#   aws ssm put-parameter \
#     --name "$(terraform output -raw anthropic_api_key_parameter_name)" \
#     --key-id "$(terraform output -raw secrets_kms_key_id)" \
#     --type SecureString --overwrite \
#     --value "sk-ant-..."
#
# The `ignore_changes` below is what makes that stick: after the first apply Terraform
# stops looking at this attribute, so the real key never appears in a plan diff, never
# round-trips through state, and a later `apply` cannot revert it to the placeholder.
#
# The considered alternative was a `sensitive` variable read from the gitignored
# terraform.tfvars. It is less typing and it keeps the key under one workflow, but the
# key then exists in three more places than it needs to: the operator's tfvars file,
# the plan file, and - in cleartext, `sensitive` notwithstanding, because that flag
# only redacts CLI output - the remote state object. State lives in a shared S3 bucket
# that more principals can read than should ever see this key. The placeholder costs
# one manual command per deployment and keeps the secret in exactly one system, which
# is the one designed to hold it.
#
# Do NOT add a `data "aws_ssm_parameter"` reading this back. A data source pulls the
# decrypted value into state and undoes everything above. The Lambda reads it at
# runtime, with its own IAM, which is the point.
resource "aws_ssm_parameter" "anthropic_api_key" {
  name        = local.anthropic_api_key_parameter
  description = "Anthropic API key for the ${var.project} agent Lambda. Populated out of band - see secrets.tf."
  type        = "SecureString"
  key_id      = aws_kms_key.secrets.key_id

  # Standard tier: 4KB and free. Advanced tier is billed per parameter per month and
  # buys size and policies (expiration notifications) nothing here needs.
  tier = "Standard"

  value = "PLACEHOLDER - replace out of band, see the comment above"

  lifecycle {
    ignore_changes = [value]
  }
}
