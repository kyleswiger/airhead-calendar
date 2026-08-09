# A data source, not `resource "archive_file"` - deliberately. `terraform validate`
# never reads data sources, so CI can validate this stack with no build directory
# and no AWS credentials; the zip only has to exist by plan/apply time. Build it
# first:
#
#   ./backend/build-lambda.sh
#
# The build directory is gitignored (`build/` and `*.zip` in .gitignore).
data "archive_file" "api" {
  type        = "zip"
  source_dir  = "${path.module}/../backend/build/api"
  output_path = "${path.module}/../backend/build/api.zip"
}

# Explicit, with retention. Left implicit, Lambda creates this group on first
# invocation with retention set to "Never expire", and household PII accumulates
# in CloudWatch forever at $0.03/GB-month. The name matches Lambda's default so
# the two can never diverge into a pair of half-populated groups.
resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/lambda/${var.project}-api"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "api" {
  function_name = "${var.project}-api"
  description   = "FastAPI + Mangum: CRUD and agenda queries for ${var.project}."
  role          = aws_iam_role.api.arn

  # Contractual with backend/src/airhead/handler.py - Mangum adapter at module
  # scope, named `handler`. Changing either side without the other is a 502 that
  # only shows up at runtime.
  handler = "airhead.handler.handler"
  runtime = "python3.12"

  # arm64 is ~20% cheaper per GB-second and every dependency here publishes
  # aarch64 wheels. build-lambda.sh passes a matching --platform to pip; if you
  # change this, change LAMBDA_ARCH there too or pydantic-core imports will fail
  # on the first request with a manylinux .so mismatch.
  architectures = ["arm64"]

  # FastAPI + pydantic import is the cold start, not the request. Memory buys CPU
  # proportionally, so 512MB imports meaningfully faster than 256MB and often costs
  # less overall; the timeout is generous for a DynamoDB Query but well under API
  # Gateway's 30s ceiling, so a hung request fails as a 502 rather than a timeout
  # the client can't distinguish from a network drop.
  memory_size = var.api_memory_mb
  timeout     = var.api_timeout_seconds

  filename = data.archive_file.api.output_path

  # Without this, Terraform compares only the S3/file metadata it last recorded
  # and a rebuilt zip with the same path is a no-op apply - the classic "I deployed
  # and nothing changed" afternoon.
  source_code_hash = data.archive_file.api.output_base64sha256

  # CI (deploy.yml) pushes code out-of-band with update-function-code, and its
  # Info-ZIP artifact never hashes identically to archive_file's Go-written zip.
  # Without this, every post-CI plan proposes "reverting" the Lambda to whatever
  # stale zip sits in the local build directory. CI owns code; Terraform owns
  # configuration. Remove this only if deploys move back into Terraform.
  lifecycle {
    ignore_changes = [filename, source_code_hash]
  }

  # NO VPC CONFIGURATION, AND DO NOT ADD ONE. PRD §16 is explicit: nothing in this
  # stack needs to be inside a VPC. DynamoDB, CloudWatch, and SSM are all reached
  # over their public endpoints with SigV4 and IAM, which is exactly as authenticated
  # as reaching them privately. Attaching this function to a VPC would immediately
  # require interface endpoints (or a NAT gateway) for each of those services, and on
  # a prior project those endpoints became the single largest line on the bill - more
  # than every other resource combined - for a workload that had no private
  # subnet-resident dependency either. If a future component genuinely needs one
  # (RDS, ElastiCache), give THAT function a VPC. Not this one.

  environment {
    variables = {
      # Names are the contract with airhead.api.deps.Settings.
      AIRHEAD_TABLE        = aws_dynamodb_table.airhead.name
      AIRHEAD_HOUSEHOLD_ID = var.household_id
      AIRHEAD_TZ           = var.household_timezone
      AIRHEAD_LOG_LEVEL    = var.log_level

      # Explicit even though it is the code default: the fallback is sqlite, and a
      # Lambda that silently fell back to an in-memory database would return empty
      # agendas and 200s rather than failing loudly.
      AIRHEAD_REPO_BACKEND = "dynamodb"
    }
  }

  # JSON so CloudWatch Logs Insights can filter on fields instead of regexing
  # message text (PRD §13: structured logging, no event titles at INFO).
  logging_config {
    log_format = "JSON"
    log_group  = aws_cloudwatch_log_group.api.name
  }

  # Ensures the group exists - with its retention - before the first invocation
  # can race Lambda into creating it implicitly.
  depends_on = [aws_cloudwatch_log_group.api]
}

# --- M2: agent ---------------------------------------------------------------

# Deliberately no `data "archive_file" "agent"`. The agent is not a separate program:
# it is `POST /api/agent/turn` on the same FastAPI app, in the same `airhead` package,
# produced by the same `./backend/build-lambda.sh`. Zipping the identical directory a
# second time would give two artifacts that must be rebuilt in lockstep and would drift
# the first time someone rebuilds one - so both functions point at one archive.
#
# That also answers most of the "second deployment artifact" objection to giving the
# agent its own function (the routing note in api.tf): what M2 actually adds is a
# second *function* over the same bytes, not a second build.

resource "aws_cloudwatch_log_group" "agent" {
  name              = "/aws/lambda/${var.project}-agent"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "agent" {
  function_name = "${var.project}-agent"
  description   = "Anthropic tool loop for ${var.project}: natural language in, calendar tool calls out."
  role          = aws_iam_role.agent.arn

  # Same entry point as the api function, on purpose. API Gateway only ever routes
  # POST /api/agent/turn here (see api.tf), so the extra routes this app carries are
  # unreachable through the gateway; what the shared handler buys is that the agent
  # route is developed, tested, and imported exactly once. If the agent package later
  # grows a leaner entry point that skips the CRUD imports for cold-start reasons,
  # this line is the only thing that changes.
  handler = "airhead.handler.handler"
  runtime = "python3.12"

  # Must match the api function and build-lambda.sh's LAMBDA_ARCH - see the note there.
  # They share an artifact, so these cannot diverge even in principle.
  architectures = ["arm64"]

  # Sized against the wrong instinct. This function spends nearly all of its wall
  # clock blocked on the Anthropic API, and Lambda bills GB-seconds for time spent
  # waiting on a socket exactly like time spent computing - so doubling memory doubles
  # the cost of the dominant term and does not make the model answer one millisecond
  # sooner. Memory here buys only cold-start speed on the `anthropic` + `httpx` +
  # FastAPI import, and 512MB already covers that. Raising it is a pure cost increase.
  memory_size = var.agent_memory_mb

  # See the derivation on var.agent_timeout_seconds. Short version: a turn is 2-3
  # sequential model calls with thinking on, and the binding constraint is not this
  # number - it is API Gateway's 30s integration ceiling, which this sits just under
  # so a slow turn fails as a Lambda timeout in this function's log group rather than
  # an opaque gateway 504.
  timeout = var.agent_timeout_seconds

  # A hard ceiling on how much model spend can be in flight at once. Three people and
  # one wall display cannot legitimately have more turns running than this, and the
  # failure mode it guards against - a retry loop on the display, or the same request
  # replayed - is measured in dollars per minute at Opus pricing, not in cents like a
  # runaway DynamoDB query. Throttled invocations surface as 429s, which is the right
  # answer to "you are already asking". Set to -1 to opt out.
  reserved_concurrent_executions = var.agent_reserved_concurrency

  filename         = data.archive_file.api.output_path
  source_code_hash = data.archive_file.api.output_base64sha256

  # CI (deploy.yml) pushes code out-of-band with update-function-code, and its
  # Info-ZIP artifact never hashes identically to archive_file's Go-written zip.
  # Without this, every post-CI plan proposes "reverting" the Lambda to whatever
  # stale zip sits in the local build directory. CI owns code; Terraform owns
  # configuration. Remove this only if deploys move back into Terraform.
  lifecycle {
    ignore_changes = [filename, source_code_hash]
  }

  # NO VPC CONFIGURATION. The reasoning on the api function above applies unchanged,
  # and one thing more: this function's only outbound dependency is Bedrock's Mantle
  # endpoint (bedrock-mantle.us-east-1.api.aws), a public SigV4 endpoint. In a VPC it
  # would need a NAT gateway
  # - not an interface endpoint - which is the most expensive way this stack could
  # possibly reach the internet, on a $7-13/month budget (PRD §16).

  environment {
    variables = {
      # Same contract as the api function - airhead.api.deps.Settings reads these.
      AIRHEAD_TABLE        = aws_dynamodb_table.airhead.name
      AIRHEAD_HOUSEHOLD_ID = var.household_id
      AIRHEAD_TZ           = var.household_timezone
      AIRHEAD_LOG_LEVEL    = var.log_level
      AIRHEAD_REPO_BACKEND = "dynamodb"

      # No API key variable, and no SSM parameter name either: the function invokes
      # Claude through Bedrock's Mantle endpoint, SigV4-signed with this role's own
      # credentials, and Lambda injects AWS_REGION on its own. The whole secret
      # distribution problem this block used to document is gone - auth is the
      # bedrock:InvokeModel* grant in iam.tf, billing is the AWS bill.

      # M2 contract, "The model". Configuration rather than constants because effort
      # is the primary cost/latency lever and the contract explicitly says to sweep
      # low/medium/high against real transcripts - which is a variable change and an
      # apply, not a rebuild. Note what is absent: no temperature, top_p, or top_k,
      # which Opus 5 rejects outright, and no budget_tokens, which is removed and
      # returns a 400.
      AIRHEAD_AGENT_MODEL      = var.agent_model
      AIRHEAD_AGENT_EFFORT     = var.agent_effort
      AIRHEAD_AGENT_MAX_TOKENS = tostring(var.agent_max_tokens)
    }
  }

  # JSON for the same reason as the api function, and it matters more here: a turn
  # legitimately handles event titles, which are household PII and must not reach a
  # log line at INFO (PRD §13, M2 contract "Audit log"). The turn record in DynamoDB
  # is where the transcript belongs; CloudWatch gets ids, counts, and token usage.
  logging_config {
    log_format = "JSON"
    log_group  = aws_cloudwatch_log_group.agent.name
  }

  depends_on = [aws_cloudwatch_log_group.agent]
}
