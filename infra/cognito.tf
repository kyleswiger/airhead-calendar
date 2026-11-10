# Family sign-in (PRD §13): one Cognito user pool, admin-created users only.
#
# What it authenticates is the *household*, not the person. The kitchen screen is a
# shared device that signs in once and stays signed in; who is acting is the
# member-selection step on screen (X-Airhead-Member, resolved server-side against the
# family roster only). Cognito never sees a member id.
#
# Users are data, not infrastructure - and an aws_cognito_user here would put a
# password in state. Create them with the CLI (see README "Family sign-in").
resource "aws_cognito_user_pool" "family" {
  name = "${var.project}-family"

  # Lite: free at this scale (well under the MAU tier) and covers password auth +
  # refresh tokens, which is all the kiosk uses.
  user_pool_tier = "LITE"

  # No self-signup, ever. This pool gates a family's calendar on a public URL.
  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  # Deleting the pool signs out the kitchen screen and every phone; make it a
  # two-step decision.
  deletion_protection = "ACTIVE"

  # Usernames, not emails: the kiosk login is typed on a touchscreen.
  username_configuration {
    case_sensitive = false
  }

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = false
    require_numbers                  = true
    require_symbols                  = false
    temporary_password_validity_days = 7
  }

  # No recovery channel configured (no email/SMS), so recovery is the owner
  # running admin-set-user-password. Explicit so Cognito does not default to
  # verified-email recovery that can never be satisfied.
  account_recovery_setting {
    recovery_mechanism {
      name     = "admin_only"
      priority = 1
    }
  }

  mfa_configuration = "OFF"
}

# A public SPA client: no secret (a browser cannot keep one), USER_PASSWORD_AUTH from
# the launch screen, and long-lived refresh tokens so a wall display survives months
# unattended. The client id is not a secret - /api/auth/config serves it.
resource "aws_cognito_user_pool_client" "kiosk" {
  name         = "${var.project}-kiosk"
  user_pool_id = aws_cognito_user_pool.family.id

  generate_secret = false
  explicit_auth_flows = [
    "ALLOW_USER_PASSWORD_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
  ]

  # Short access tokens bound the blast radius of one leaking from the page; the
  # refresh token is what keeps the screen signed in, and it is revocable
  # (admin-user-global-sign-out, or Sign out on the screen).
  access_token_validity  = 60
  id_token_validity      = 60
  refresh_token_validity = var.family_session_days
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }

  enable_token_revocation       = true
  prevent_user_existence_errors = "ENABLED"
}
