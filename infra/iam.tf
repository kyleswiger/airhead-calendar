# One role per Lambda, never a shared one. PRD §13 requires that the `sync` role
# cannot invoke the model and the `agent` role cannot read OAuth tokens, and
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

# The api role has no bedrock:InvokeModel, no ssm:GetParameter, and no kms:Decrypt.
# It needs none of them: model access belongs to the agent role and OAuth refresh
# tokens belong to the sync role. Grant those on the role that uses them, never here
# for convenience.

# --- M2: agent ---------------------------------------------------------------

# The second role, built by copying the block above rather than widening it - which
# is what the note at the top of this file promised and the only reason PRD §13's
# split is real. Concretely: the api role still cannot invoke the model, and when
# M3 adds `sync`, that role will not be able to either.
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

# Model access is IAM, not a key. The agent invokes Claude through Bedrock's Mantle
# endpoint with the role's own SigV4 credentials - there is no Anthropic API key, no
# SSM parameter holding one, and no KMS key protecting it. What remains of PRD §13's
# role separation is this grant: only the agent role may invoke the model, scoped to
# exactly one model.
#
# anthropic.claude-opus-5 is INFERENCE_PROFILE-only in us-east-1 (direct
# foundation-model invoke is rejected), so the code sends the `us.` cross-region
# profile id and the grant must cover BOTH the profile ARN (what the request names)
# and the regional foundation-model ARNs it fans out to (what Bedrock invokes on the
# caller's behalf). Dropping either half fails at the first turn with an AccessDenied
# naming whichever ARN is missing.
data "aws_iam_policy_document" "agent_bedrock" {
  statement {
    sid    = "InvokeClaudeOpus5"
    effect = "Allow"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = [
      "arn:aws:bedrock:${var.region}:${data.aws_caller_identity.current.account_id}:inference-profile/us.anthropic.claude-opus-5",
      # The profile's member models - us-east-1, us-east-2, us-west-2 today. A region
      # wildcard on this one model id, rather than three pinned regions, so Bedrock
      # adding a member region is not a mid-conversation AccessDenied.
      "arn:aws:bedrock:*::foundation-model/anthropic.claude-opus-5",
    ]
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

resource "aws_iam_role_policy" "agent_bedrock" {
  name   = "bedrock"
  role   = aws_iam_role.agent.id
  policy = data.aws_iam_policy_document.agent_bedrock.json
}

# The agent role has no route to an OAuth refresh token: no ssm:GetParameter at all,
# no kms:Decrypt, and its only unusual grant is bedrock:InvokeModel* on one model.
# That is the half of PRD §13 M2 can prove. The other half - the sync role not being
# able to invoke the model - is M3's to keep, and it stays true as long as its policy
# names its own parameters and nothing under bedrock:*.
