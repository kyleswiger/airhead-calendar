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

## M1: the control plane

M1 adds the DynamoDB table, the `api` Lambda, and an API Gateway HTTP API. The Lambda ships
as a zip built from source, so **build before you apply** — Terraform reads the package with
`data "archive_file"`, which needs the build directory to exist at plan time.

```bash
./backend/build-lambda.sh     # → backend/build/api/  (gitignored)

cd infra
terraform init -backend-config=backend.hcl
terraform apply
```

The build cross-compiles for `arm64` via pip's `--platform`, so it works from any machine —
but if you change `architectures` on the Lambda, change `LAMBDA_ARCH` in the script to match
or `pydantic-core` fails to import on the first request.

The frontend reads the API host at **build** time, not runtime, so it needs the output baked
in before `deploy.sh` runs:

```bash
export VITE_API_BASE=$(terraform -chdir=infra output -raw api_base_url)
./deploy.sh
```

M1 uses the raw `execute-api` URL; a custom domain is a later milestone. CORS allows exactly
`https://<subdomain>.<root_domain>`, so the API is not reachable from a page served anywhere
else — including `localhost` during frontend development. Run the backend locally
(`AIRHEAD_REPO_BACKEND=sqlite uvicorn airhead.api:app`) rather than pointing a dev server at
the deployed API.

## M2: the agent

M2 adds a second Lambda (`airhead-agent`), the `POST /api/agent/turn` route in front of
it, a KMS key, and an SSM `SecureString` parameter holding the Anthropic API key.

**The key is not in Terraform.** Terraform creates the parameter with a placeholder and
then stops looking at the value (`ignore_changes`), so the real key never enters a plan,
a `.tfvars`, or the state file. You put it there once, by hand, after the first apply:

```bash
cd infra
terraform apply

aws ssm put-parameter \
  --name "$(terraform output -raw anthropic_api_key_parameter_name)" \
  --key-id "$(terraform output -raw secrets_kms_key_id)" \
  --type SecureString --overwrite \
  --value "sk-ant-..."
```

Pass `--key-id`. `--overwrite` without it can land the value under a different key than
the agent role is allowed to decrypt, and the first turn then fails with an AccessDenied
that names KMS rather than the mistake.

The agent Lambda ships from the **same build as the api Lambda** — same `airhead` package,
same `airhead.handler.handler`, same zip. `./backend/build-lambda.sh` still builds one
artifact; M2 adds a second function over it, with its own role, timeout, and log group.
It exists separately so that the role serving CRUD never holds the Anthropic key, and so
that a 25-second model turn and a 15-second timeout on a DynamoDB query can coexist.

Tuning lives in `variables.tf` under the M2 divider — `agent_effort` (`low`/`medium`/`high`)
is the cost and latency lever worth sweeping first; `agent_max_tokens` caps thinking *plus*
reply on Opus 5, so it is not a reply-length setting.

`agent_reserved_concurrency` defaults to 3. It is a spend cap, not a performance setting:
a retry loop on the kitchen display costs real money per minute at Opus pricing, and past
three simultaneous turns in a three-person household something is wrong.

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
