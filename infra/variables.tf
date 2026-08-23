variable "project" {
  description = "Resource name prefix. NEVER change this after apply - it prefixes every resource name."
  type        = string
  default     = "airhead"
}

variable "region" {
  description = "Primary AWS region."
  type        = string
  default     = "us-east-1"
}

variable "root_domain" {
  description = "Apex domain you already own. The hosted zone is looked up by name, never created."
  type        = string
}

variable "subdomain" {
  description = "Subdomain the kitchen display and PWA are served from."
  type        = string
  default     = "airhead"
}

# --- M1: control plane -------------------------------------------------------

variable "household_id" {
  description = "The single household's id. Forms the `HH#<id>` partition key prefix on every item - changing it after seeding orphans all existing data, so treat it like var.project."
  type        = string
  default     = "hh_1"
}

variable "household_timezone" {
  description = "IANA zone the kitchen display renders in. Events store their originating zone; this is what 'today' means."
  type        = string
  default     = "America/New_York"
}

variable "log_level" {
  description = "Application log level for the api Lambda. DEBUG logs event titles, which are household PII - do not leave it there."
  type        = string
  default     = "INFO"

  validation {
    condition     = contains(["DEBUG", "INFO", "WARNING", "ERROR"], var.log_level)
    error_message = "log_level must be one of DEBUG, INFO, WARNING, ERROR (Python logging names)."
  }
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for the Lambda and API access logs. Never leave this unset - the default is 'never expire'."
  type        = number
  default     = 30
}

variable "api_memory_mb" {
  description = "Memory for the api Lambda. Memory buys CPU proportionally, so this is really the cold-start-speed dial for the FastAPI import."
  type        = number
  default     = 512
}

variable "api_timeout_seconds" {
  description = "Timeout for the api Lambda. Keep below API Gateway's 30s integration ceiling so the function, not the gateway, is what fails."
  type        = number
  default     = 15

  validation {
    condition     = var.api_timeout_seconds > 0 && var.api_timeout_seconds < 30
    error_message = "api_timeout_seconds must be under 30 - API Gateway HTTP APIs cap the integration at 30s."
  }
}

variable "api_throttle_rate_limit" {
  description = "Steady-state requests/second across the whole API. Sized for a three-person household plus a wall display, with headroom."
  type        = number
  default     = 20
}

variable "api_throttle_burst_limit" {
  description = "Burst bucket depth for the API. Absorbs a round of simultaneous tab refreshes without letting a retry loop run away."
  type        = number
  default     = 40
}

# --- M2: agent ---------------------------------------------------------------
#
# There is no Anthropic key anywhere in this stack. The agent invokes Claude through
# Bedrock with its Lambda role's own SigV4 credentials - see the bedrock grant in
# iam.tf - so there is nothing secret to hold, distribute, or rotate.

variable "agent_memory_mb" {
  description = "Memory for the agent Lambda. This function waits on the model far more than it computes, and waiting is billed as GB-seconds - so this is a cold-start dial with a direct cost penalty, not a speed dial."
  type        = number
  default     = 512
}

variable "agent_timeout_seconds" {
  description = "Timeout for the agent Lambda. Sized for a multi-call tool loop with thinking on; the 30s HTTP API integration ceiling is the real limit, so this must stay under it."
  type        = number
  default     = 28

  # Where 28 comes from, so the next person can re-derive it instead of guessing:
  #
  #   one Opus 5 turn, effort=medium, thinking on   ~4-8s
  #   a typical write ("add soccer thursday at 4")  = 2 model turns + 1 tool call
  #   a confirmed delete or a conflict check        = 3 model turns
  #   tool calls themselves (DynamoDB Query/Put)    < 100ms, not the term that matters
  #
  # So p50 is ~8-12s and a three-turn tail is ~20-25s. 28 covers that tail with two
  # seconds of margin under the gateway, which is deliberate: the function must be the
  # thing that times out, so the failure appears in its own log group with the turn's
  # context, not as a bare 504 the client cannot distinguish from a network drop.
  #
  # If turns genuinely need longer than this, the answer is not a bigger number - the
  # ceiling is API Gateway's, not Lambda's. It is streaming (a Lambda Function URL with
  # response streaming) or an async job the display polls. Both are M2.5 conversations.
  validation {
    condition     = var.agent_timeout_seconds > 0 && var.agent_timeout_seconds < 30
    error_message = "agent_timeout_seconds must be under 30 - API Gateway HTTP APIs cap the integration at 30s, so a larger value cannot take effect."
  }
}

variable "agent_reserved_concurrency" {
  description = "Concurrent agent invocations allowed. A hard ceiling on in-flight model spend; -1 disables the reservation and lets the function draw on the account pool."
  type        = number
  default     = 3

  validation {
    condition     = var.agent_reserved_concurrency == -1 || var.agent_reserved_concurrency >= 1
    error_message = "agent_reserved_concurrency must be -1 (unreserved) or at least 1 - 0 disables the function entirely."
  }
}

variable "agent_model" {
  description = "Bedrock model id for the conversational agent, invoked through the legacy bedrock-runtime InvokeModel path (this account is not onboarded to bedrock-mantle, and Opus 5 is sales-gated). This is the `us.` cross-region inference profile id, not the bare foundation-model id. Changing it means also changing the ARN pair in iam.tf's agent_bedrock policy."
  type        = string
  default     = "us.anthropic.claude-sonnet-4-6"
}

variable "agent_effort" {
  description = "Model thinking depth (output_config.effort). The primary cost and latency lever - the M2 contract says to sweep low/medium/high against real transcripts, and this is the knob that sweep turns."
  type        = string
  default     = "medium"

  validation {
    condition     = contains(["low", "medium", "high"], var.agent_effort)
    error_message = "agent_effort must be low, medium, or high. Opus 5 removed budget_tokens - depth is set with effort, and a budget_tokens parameter returns a 400."
  }
}

variable "agent_max_tokens" {
  description = "max_tokens for an agent turn. On Opus 5 this caps thinking PLUS response text, so sizing it around the visible answer truncates the answer once thinking is counted."
  type        = number
  default     = 16384

  # 16k is the PRD §10 figure for the non-streaming path; the 64k figure there is for
  # streaming to the screen, which this synchronous route does not do. A reply to
  # "add soccer thursday at 4" is a couple of hundred tokens - essentially all of this
  # budget is headroom for thinking, and unused budget is not billed.
  validation {
    condition     = var.agent_max_tokens >= 4096
    error_message = "agent_max_tokens must be at least 4096 - thinking counts against this ceiling, so a small value truncates the reply mid-sentence rather than producing a short one."
  }
}
