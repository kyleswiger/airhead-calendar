# One role per Lambda, never a shared one. PRD §13 requires that the `sync` role
# cannot read the Anthropic key and the `agent` role cannot read OAuth tokens, and
# that separation only exists if the roles are separate from the start - retrofitting
# it means re-deriving which of a shared role's permissions each function actually
# used, from logs, after the fact.
#
# M1 ships the `api` role only. Adding `agent`, `sync`, `classify`, and `sms` means
# copying the four-resource block below (role, log policy, data policy, attachment)
# and giving each its own SSM path. The assume-role document is deliberately shared -
# it grants nothing beyond "Lambda may assume this" and is identical for every role.
data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "api" {
  name               = "${var.project}-api"
  description        = "Execution role for the ${var.project} api Lambda (FastAPI CRUD + agenda)."
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

# Deliberately NOT AWSLambdaBasicExecutionRole: that managed policy grants logs:*
# on every log group in the account. This function writes to exactly one group.
#
# logs:CreateLogGroup is absent because Terraform creates the group (see lambda.tf)
# with a retention policy. If this permission is ever added back, a deleted group
# silently reappears with unlimited retention and the retention setting is lost.
data "aws_iam_policy_document" "api_logs" {
  statement {
    sid       = "WriteOwnLogStreams"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.api.arn}:*"]
  }
}

# Scoped to this one table and its indexes. Not dynamodb:*, not Resource = "*".
#
# dynamodb:Scan is absent by design: every read path in the repo layer is keyed by
# household (base table) or by source/member (GSI1/GSI2), so a Scan here would only
# ever be an accident - and an accidental Scan on this table is a full read of the
# household's calendar, billed and logged as such.
data "aws_iam_policy_document" "api_dynamodb" {
  statement {
    sid    = "TableItemAccess"
    effect = "Allow"
    actions = [
      "dynamodb:DeleteItem",
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:Query",
      "dynamodb:UpdateItem",

      # boto3's Table resource issues this lazily the first time it needs the key
      # schema; without it the first write of a cold container fails, not the read
      # that looks like it caused it.
      "dynamodb:DescribeTable",

      # A single event write in repo/dynamo.py touches three items (canonical,
      # id pointer, recurrence mirror). Today that is three calls with a window
      # where they disagree; making it atomic is a code change, and pre-granting
      # the action keeps that from also being an IAM change under a live bug.
      "dynamodb:ConditionCheckItem",
      "dynamodb:TransactGetItems",
      "dynamodb:TransactWriteItems",
    ]
    resources = [aws_dynamodb_table.airhead.arn]
  }

  # Indexes are read-only surfaces - writes go to the base table and DynamoDB
  # propagates them - so the index statement carries only the query actions.
  statement {
    sid       = "IndexQueryAccess"
    effect    = "Allow"
    actions   = ["dynamodb:Query"]
    resources = ["${aws_dynamodb_table.airhead.arn}/index/*"]
  }
}

resource "aws_iam_role_policy" "api_logs" {
  name   = "logs"
  role   = aws_iam_role.api.id
  policy = data.aws_iam_policy_document.api_logs.json
}

resource "aws_iam_role_policy" "api_dynamodb" {
  name   = "dynamodb"
  role   = aws_iam_role.api.id
  policy = data.aws_iam_policy_document.api_dynamodb.json
}

# The api role has no ssm:GetParameter and no kms:Decrypt. It needs neither: the
# Anthropic key belongs to the agent role and OAuth refresh tokens belong to the
# sync role. Grant secrets on the role that uses them, never here for convenience.

# --- M2: agent ---------------------------------------------------------------

# The second role, built by copying the block above rather than widening it - which
# is what the note at the top of this file promised and the only reason PRD §13's
# split is real. Concretely: the api role still cannot read the Anthropic key, and
# when M3 adds `sync`, that role will not be able to either.
resource "aws_iam_role" "agent" {
  name               = "${var.project}-agent"
  description        = "Execution role for the ${var.project} agent Lambda (Anthropic tool loop)."
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

# Same reasoning as api_logs: no managed policy, no logs:CreateLogGroup, one group.
data "aws_iam_policy_document" "agent_logs" {
  statement {
    sid       = "WriteOwnLogStreams"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.agent.arn}:*"]
  }
}

# THE STATEMENT THIS WHOLE FILE EXISTS FOR. Read the Resource before you edit it.
#
# It is a single parameter ARN. Not `ssm:*`, not `Resource = "*"`, and - the one that
# looks harmless and is not - not `arn:aws:ssm:*:*:parameter/airhead/*`. A wildcard on
# the project path reads as "this stack's own secrets" and is fine on the day it is
# written, because the Anthropic key is the only parameter under it. M3 stores Google
# OAuth refresh tokens under the same prefix, and on that day the path grant silently
# becomes "the agent may read the household's calendar credentials" - with no diff on
# this file, no failing test, and nothing in a plan to notice. PRD §13 asks for exactly
# the opposite. Naming the parameter outright means a future secret is a deliberate
# edit here, not an accident of prefix matching.
#
# GetParameter, singular: boto3's get_parameter() calls it, and GetParametersByPath is
# a listing API - the one call that would enumerate the sync role's parameters.
data "aws_iam_policy_document" "agent_secrets" {
  statement {
    sid       = "ReadAnthropicKey"
    effect    = "Allow"
    actions   = ["ssm:GetParameter"]
    resources = [aws_ssm_parameter.anthropic_api_key.arn]
  }

  # SSM decrypts a SecureString with the caller's own KMS permissions, so without this
  # GetParameter(WithDecryption=True) fails with AccessDenied naming KMS, not SSM.
  #
  # The encryption context condition is the belt to the key ARN's braces. SSM passes
  # `PARAMETER_ARN = <the parameter's arn>` on every SecureString decrypt, so pinning
  # it means this grant covers this parameter and nothing else - including the future
  # parameters that will share this key. If the M3 tokens land on the same CMK, the
  # agent still cannot decrypt them, because the context will not match.
  statement {
    sid       = "DecryptAnthropicKey"
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.secrets.arn]

    condition {
      test     = "StringEquals"
      variable = "kms:EncryptionContext:PARAMETER_ARN"
      values   = [aws_ssm_parameter.anthropic_api_key.arn]
    }
  }
}

# Same table, same shape, same omissions as api_dynamodb - the agent's tools run
# through the same repo layer, so it needs the same actions and no more. Notably it
# also has no dynamodb:Scan: the agent is the caller most likely to be *asked* for
# something that sounds like a scan ("what's on everyone's calendar this year"), and
# the answer is a bounded Query per member, not a table read.
#
# It writes AgentTurn items (PRD §7, M2 contract "Audit log") to the same PK as the
# rest of the household, so the audit log needs no separate grant - and could not be
# given one, since IAM cannot scope DynamoDB by SK prefix.
data "aws_iam_policy_document" "agent_dynamodb" {
  statement {
    sid    = "TableItemAccess"
    effect = "Allow"
    actions = [
      "dynamodb:DeleteItem",
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:Query",
      "dynamodb:UpdateItem",
      "dynamodb:DescribeTable",
      "dynamodb:ConditionCheckItem",
      "dynamodb:TransactGetItems",
      "dynamodb:TransactWriteItems",
    ]
    resources = [aws_dynamodb_table.airhead.arn]
  }

  # GSI1 (source/external id) and GSI2 (member/day). The agent reads GSI2 on every
  # get_agenda and find_conflicts call; GSI1 is here because merge_events resolves
  # provider identities. Query only - index writes do not exist.
  statement {
    sid       = "IndexQueryAccess"
    effect    = "Allow"
    actions   = ["dynamodb:Query"]
    resources = ["${aws_dynamodb_table.airhead.arn}/index/*"]
  }
}

resource "aws_iam_role_policy" "agent_logs" {
  name   = "logs"
  role   = aws_iam_role.agent.id
  policy = data.aws_iam_policy_document.agent_logs.json
}

resource "aws_iam_role_policy" "agent_dynamodb" {
  name   = "dynamodb"
  role   = aws_iam_role.agent.id
  policy = data.aws_iam_policy_document.agent_dynamodb.json
}

resource "aws_iam_role_policy" "agent_secrets" {
  name   = "secrets"
  role   = aws_iam_role.agent.id
  policy = data.aws_iam_policy_document.agent_secrets.json
}

# The agent role has no route to an OAuth refresh token: no ssm:GetParameter beyond
# the one ARN above, no ssm:GetParametersByPath to discover others, and a kms:Decrypt
# that only unlocks one encryption context. That is the half of PRD §13 M2 can prove.
# The other half - the sync role not reaching the Anthropic key - is M3's to keep, and
# it stays true as long as its policy names its own parameters the same way.
