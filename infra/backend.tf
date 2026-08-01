terraform {
  required_version = ">= 1.9"

  # Partial backend configuration. The bucket and lock table are deployment
  # specific and are not committed - supply them at init time:
  #
  #   cp backend.hcl.example backend.hcl   # fill in, gitignored
  #   terraform init -backend-config=backend.hcl
  backend "s3" {}

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }
}
