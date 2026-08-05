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
