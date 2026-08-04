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
