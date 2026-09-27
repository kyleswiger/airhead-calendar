# CLAUDE.md — airhead-calendar

Family calendar for a wall-mounted kitchen touchscreen: relevance tiers (work noise collapses to a
busy band) + an agentic chat interface (Claude via Bedrock). Public repo, no deployment values committed.
Spec: `docs/PRD.md`; milestone contracts: `docs/M1-CONTRACT.md`, `docs/M2-CONTRACT.md`, `docs/ROUTINES-CONTRACT.md`.

## Layout
- `backend/` — Python 3.12+, FastAPI + Mangum. `src/airhead/`: `api/` (routes, `deps.py` = env Settings),
  `agent/` (prompt, tool loop, tools), `repo/` (`sqlite.py` / `dynamo.py` behind `base.py`, `turns.py`),
  `routines/`, `domain.py`, `recurrence.py`, `handler.py` (Lambda entry `airhead.handler.handler`).
- `frontend/` — Vite + React 19 + TS. `src/api.ts` picks real API vs bundled fixtures.
- `infra/` — Terraform, single stack (S3+CloudFront site, DynamoDB, 2 Lambdas, HTTP API, GitHub OIDC role).
- `kiosk/` — placeholder README only (M5). `deploy.sh` — manual frontend ship from TF outputs.

## Commands (lint/test/typecheck/validate mirror ci.yml)
```bash
# backend (cd backend)
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/pytest --cov=airhead            # hermetic: SQLite + moto, no AWS
.venv/bin/python dev_server.py            # :8000, seeded SQLite, localhost CORS
./build-lambda.sh                          # -> backend/build/api/ (arm64 wheels, py3.12)

# frontend (cd frontend)
npm ci && npm run typecheck && npm run build
VITE_API_BASE=http://localhost:8000 npm run dev   # unset VITE_API_BASE = fixture mode

# infra (cd infra)
terraform fmt -check -recursive && terraform init -backend=false && terraform validate
```

## CI/CD (`.github/workflows/`)
- `ci.yml` (PR + push main): backend ruff/pytest on py3.13, frontend typecheck+build on node 22,
  terraform fmt/validate (TF 1.9.6, no creds).
- `deploy.yml` (push to main, or `workflow_dispatch` = ship both): paths-filtered. Backend → builds
  once, `update-function-code` on `airhead-api` and `airhead-agent`. Frontend → resolves API URL /
  bucket (`airhead-site-<account>`) / CloudFront id (by comment) from the live account, builds, syncs S3,
  invalidates. **Merge to main = prod deploy; no approval gate, no staging env.**
- `release.yml`: release-please (`simple`) on main → release PRs + `CHANGELOG.md`.
- `secret-scan.yml`: gitleaks (config `.gitleaks.toml`); false positives → `gitleaks:allow` or allowlist.
- `claude.yml` (@claude mentions) and `claude-code-review.yml` (auto review on every PR).
- Terraform is **never** run by CI. Infra changes = manual `terraform apply` in `infra/` by the owner.

## Environments / config
- One env (prod), us-east-1. Site: `https://<subdomain>.<root_domain>`; API: raw `execute-api` URL.
- Gitignored local files: `infra/backend.hcl` (state bucket/lock table), `infra/terraform.tfvars`
  (`root_domain`, optional `create_github_oidc_provider=false`). Copy from the `.example` files.
- GitHub secrets: `AWS_GITHUB_ACTIONS_ROLE_ARN` (OIDC role, from `terraform output`),
  `CLAUDE_CODE_OAUTH_TOKEN` (Claude workflows).
- Lambda env (set only by `infra/lambda.tf`; names are the contract with `api/deps.py`):
  `AIRHEAD_TABLE`, `AIRHEAD_HOUSEHOLD_ID`, `AIRHEAD_TZ`, `AIRHEAD_LOG_LEVEL`, `AIRHEAD_REPO_BACKEND`,
  `AIRHEAD_AGENT_MODEL`, `AIRHEAD_AGENT_EFFORT`, `AIRHEAD_AGENT_MAX_TOKENS`. Local-only: `AIRHEAD_SQLITE_PATH`.
- Frontend build-time: `VITE_API_BASE`, `VITE_MEMBER_ID`.
- No model API key: agent uses Bedrock via the Lambda role (`bedrock:InvokeModel*` in `iam.tf`).
  Future OAuth/CalDAV secrets go in SSM SecureString, never tfvars.

## Gotchas
- Never change `var.project` or `var.household_id` after apply (resource names / DynamoDB PK prefix).
- `dns.tf` looks up the hosted zone via data source; never create one.
- No VPC on any Lambda (NAT/endpoint cost). Don't add one.
- Run `build-lambda.sh` before `terraform plan/apply` — `archive_file` needs `backend/build/api/`.
  `LAMBDA_ARCH`/`PYTHON_VERSION` in the script must match the Lambda's `architectures`/runtime.
- `lambda.tf` ignores `filename`/`source_code_hash`: CI owns code, Terraform owns config. The CI role
  cannot change Lambda config/env — env var changes need an apply.
- Vite inlines `VITE_API_BASE` at build; if unset the build succeeds and silently ships fixtures.
- Deployed CORS allows only the site origin; for local frontend dev use `dev_server.py`, not the real API.
- `agent_model` change also requires updating the ARN pair in `iam.tf`'s bedrock policy. Opus 5 /
  Mantle are gated on this account — stay on legacy `AnthropicBedrock` + `us.anthropic.claude-sonnet-4-6`.
- Lambda timeouts must stay < 30s (API Gateway integration cap; validated in `variables.tf`).
- `agent_reserved_concurrency` (default 3) is a spend cap, not a perf knob.
- OIDC trust uses the immutable `sub` claim (`repo:kyleswiger@<id>/airhead-calendar@<id>:...`);
  see comments in `infra/github-actions.tf` before touching subject claims.
- `.gitignore` ignores `.claude/` and `build/`; `deploy.sh` and CI both use `npm ci` — keep it that way.
- Stacked PRs: delete the base branch on merge so GitHub retargets the child PR.
