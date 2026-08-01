provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
      Repo      = "airhead-calendar"
    }
  }
}

# CloudFront requires its ACM certificate in us-east-1. The stack already lives
# there, but the alias makes the requirement explicit so a future region change
# to var.region does not silently break certificate issuance.
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"

  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
      Repo      = "airhead-calendar"
    }
  }
}
