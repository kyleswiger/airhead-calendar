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
