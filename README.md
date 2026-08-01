# Airhead Calendar

> Gets everything out of my airy head and into the calendar.

A family calendar for a wall-mounted kitchen touchscreen, built around two ideas that
off-the-shelf calendars don't do: **relevance tiers** (work noise collapses to one band,
household commitments stay legible) and an **agentic interface** (talk or type to add,
move, and query events).

Full spec: [`docs/PRD.md`](docs/PRD.md).

## Why

Combined family calendars become unreadable. One person's work calendar alone can carry five
meetings a day; a week view across three people is dense enough that nobody can answer the
only question that matters — *who is doing what, and does it affect me?*

Existing open-source calendars solve storage and sync well. None solve signal. That's the gap
this fills: every event is classified by household impact, and the default view shows only
what affects the household. Work blocks collapse into a single "busy 9:00–15:00 (5)" band
that expands on tap.

## Layout

| Path | What |
|---|---|
| `backend/` | Python 3.12+ · FastAPI · Mangum on Lambda. Domain model, API, agent loop, sync adapters |
| `frontend/` | Vite · React · TypeScript. Kitchen display + PWA |
| `infra/` | Terraform. Single stack, S3 + CloudFront now, API/data layer next |
| `kiosk/` | Display-device provisioning (Chromium kiosk, watchdog, power management) |
| `docs/` | PRD and ADRs |

## Development

```bash
# backend
cd backend
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest

# frontend
cd frontend
npm ci && npm run dev
```

## Deploying your own

This repo carries no deployment-specific values. You supply them in two gitignored files.

```bash
cd infra
cp backend.hcl.example backend.hcl              # your TF state bucket + lock table
cp terraform.tfvars.example terraform.tfvars    # your domain

terraform init -backend-config=backend.hcl
terraform plan
terraform apply
```

Then ship the site:

```bash
./deploy.sh
```

Prerequisites: an AWS account, a domain registered in Route 53 with its hosted zone already
present, and an S3 bucket + DynamoDB table for Terraform state.

## Things that will bite you

- **`project` must never change after the first apply.** It prefixes every resource name;
  changing it is a full teardown and rebuild.
- **`dns.tf` must never create a hosted zone.** The registrar creates one at domain
  registration and delegates to it. A second zone for the same name resolves for nobody —
  so this uses `data "aws_route53_zone"`. Keep it that way, including in any future module.
- **No Lambda gets a VPC attachment.** Nothing here needs to be inside a VPC, and VPC
  endpoints are an expensive way to find that out.
- **Secrets live in SSM Parameter Store as SecureString** — OAuth refresh tokens, model API
  keys, CalDAV app passwords. Never in the repo, never in `.tfvars`.
- **`deploy.sh` uses `npm ci`, not `npm install`.** A stale `node_modules` beside an updated
  `package.json` fails at build time with an unresolvable-import error that looks like broken
  source.
- **Branch from `main`, and delete the base branch when merging a stack.** GitHub only
  auto-retargets a stacked PR when its base is deleted; otherwise the stacked PR merges into
  a dead branch, reports MERGED, and the code is nowhere.

## Status

M0 complete — infrastructure, CI, and a placeholder display. See the milestone table in the
PRD for what's next.

## License

MIT
