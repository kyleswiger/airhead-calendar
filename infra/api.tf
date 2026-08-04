# HTTP API, not REST API: ~70% cheaper per million requests, native payload v2, and
# none of the features REST APIs add (request validation, API keys, WAF integration)
# apply here - FastAPI validates, and authorization is server-side in the handler.
resource "aws_apigatewayv2_api" "api" {
  name          = "${var.project}-api"
  description   = "${var.project} control plane - CRUD, agenda, and (from M2) the agent."
  protocol_type = "HTTP"

  # Exactly the site origin. Not "*": the browser is the only client that reaches
  # this from a page, and a wildcard here would let any page a household member has
  # open issue credentialed-adjacent requests against the calendar.
  #
  # API Gateway answers OPTIONS preflight itself when CORS is configured, ahead of
  # the $default route - so preflight never reaches Lambda and FastAPI does not need
  # its own CORSMiddleware. Adding one would produce duplicate
  # Access-Control-Allow-Origin headers, which browsers reject outright.
  cors_configuration {
    allow_origins = ["https://${local.fqdn}"]
    allow_methods = ["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"]

    # x-airhead-member is the M1 actor shim (see airhead.api.deps.get_actor); it is
    # a custom header, so without it here every write is blocked at preflight.
    # authorization is listed ahead of the Cognito authorizer landing in a later
    # milestone - harmless now, one fewer thing to remember then.
    allow_headers = ["authorization", "content-type", "x-airhead-member"]

    # Cache the preflight for a day. The kitchen display makes the same handful of
    # cross-origin calls forever; re-preflighting them is pure latency.
    max_age = 86400
  }
}

resource "aws_apigatewayv2_integration" "api" {
  api_id           = aws_apigatewayv2_api.api.id
  integration_type = "AWS_PROXY"
  integration_uri  = aws_lambda_function.api.invoke_arn

  # v2 is what Mangum expects and what the $default route emits. v1 would arrive as
  # a differently-shaped event and Mangum would misread the path.
  payload_format_version = "2.0"

  # Left at the 30s default deliberately: the Lambda's own timeout is shorter, so
  # the function is always the binding constraint and a slow request surfaces as a
  # Lambda timeout in the function's logs rather than an opaque gateway 504.
}

# One catch-all route. FastAPI already owns routing, and mirroring every path into
# Terraform would mean a `terraform apply` for every new endpoint.
resource "aws_apigatewayv2_route" "default" {
  api_id    = aws_apigatewayv2_api.api.id
  route_key = "$default"
  target    = "integrations/${aws_apigatewayv2_integration.api.id}"
}

resource "aws_cloudwatch_log_group" "api_gw" {
  name              = "/aws/apigateway/${var.project}-api"
  retention_in_days = var.log_retention_days
}

# The $default stage serves from the API root, so the invoke URL carries no stage
# path segment and the frontend's VITE_API_BASE needs no `/prod` suffix.
resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.api.id
  name        = "$default"
  auto_deploy = true

  # JSON, matching the Lambda's log format, so both sides of a request can be
  # correlated on requestId in a single Logs Insights query. No request body and no
  # headers: event titles are household PII (PRD §13) and X-Airhead-Member is an
  # identity claim - neither belongs in an access log.
  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_gw.arn

    format = jsonencode({
      requestId               = "$context.requestId"
      ip                      = "$context.identity.sourceIp"
      requestTime             = "$context.requestTime"
      httpMethod              = "$context.httpMethod"
      routeKey                = "$context.routeKey"
      path                    = "$context.path"
      status                  = "$context.status"
      protocol                = "$context.protocol"
      responseLength          = "$context.responseLength"
      responseLatency         = "$context.responseLatency"
      integrationStatus       = "$context.integrationStatus"
      integrationErrorMessage = "$context.integrationErrorMessage"
    })
  }

  default_route_settings {
    # Three people, one wall display, and a 15-minute sync. Real steady-state load
    # is single-digit requests per minute, so these limits are ~100x headroom for a
    # burst of tab refreshes while still capping a runaway retry loop or a scraper
    # before it turns a $1/month API into a surprise. Raise deliberately, not
    # reflexively - a client hitting this ceiling is usually a client bug.
    throttling_rate_limit  = var.api_throttle_rate_limit
    throttling_burst_limit = var.api_throttle_burst_limit

    # Per-route CloudWatch metrics are billed per metric and there is exactly one
    # route; the stage-level metrics AWS emits for free already cover it.
    detailed_metrics_enabled = false
  }
}

# Resource-based permission, scoped to this API. Without source_arn any API Gateway
# API in the account could invoke the function.
resource "aws_lambda_permission" "api_gw" {
  statement_id  = "AllowInvokeFromHttpApi"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.api.execution_arn}/*/*"
}

# M1 ships the raw execute-api hostname. That is a deliberate scope cut, not an
# oversight: a custom domain (api.<root_domain>) needs its own regional ACM
# certificate, an aws_apigatewayv2_domain_name, an api_mapping, and a Route 53
# alias - four resources and a certificate validation wait, to save typing a
# hostname the frontend reads from a Terraform output anyway.
#
# The upgrade path is additive and does not touch anything above: add those four
# resources, then point the api_base_url output at the custom domain. Existing
# clients keep working because the execute-api URL stays live alongside it - so
# the cutover is a frontend rebuild, not a coordinated switch.
