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
