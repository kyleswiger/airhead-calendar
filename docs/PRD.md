# Airhead — Family Calendar PRD

**Owner:** @kyleswiger
**Status:** Draft v1 — 2026-08-01
**Household:** two adults (Alex = admin, Sam) and one minor (Riley, 11).
Names throughout this document are placeholders.

---

## 1. Problem

Combined family calendars become unreadable. Alex's work calendar alone averages 5 meetings/day; a
week view of all three calendars is visually dense enough that nobody can answer the only question
that matters: **"who is doing what, and does it affect me?"**

Existing FOSS calendars (Radicale, Nextcloud Calendar, Baïkal, etc.) solve storage and sync. None
solve *signal*. That's the gap.

## 2. What we do differently

1. **Relevance tiers, not more filters.** Every event is classified by household impact. The default
   kitchen view shows only what affects the household; personal/work blocks collapse into a single
   busy band per person.
2. **Agentic interface.** Natural language is the primary input on the kitchen screen: add, delete,
   move, and query events by talking or typing. No form-filling on a touchscreen while holding a pan.
3. **Automatic dedup/merge.** The same soccer game imported from Sam's Google and Alex's Outlook
   becomes one event, not two.

## 3. Goals / Non-goals

**Goals (v1)**
- Wall-mounted kitchen touchscreen showing today + next 7 days, readable from across the room.
- All three members can add/edit/delete events via agent (screen chat, screen voice, SMS).
- Read-only ingest from Google Calendar, Apple Calendar (CalDAV), Outlook (Graph), and generic ICS.
- Duplicate detection with automatic merge above a confidence threshold; review queue below it.
- Per-event visibility so adult-only items are hidden from Riley — enforced server-side.
- Runs in AWS today; the display layer is portable to a Raspberry Pi later with zero API changes.

**Non-goals (v1)**
- Writing back to external calendars (v2 — schema and adapter interface are built for it now).
- Task lists, chores, meal planning, shopping lists, photo frame mode.
- Multi-household / multi-tenant. One household, hardcoded roster of 3.
- Free/busy negotiation with people outside the household.

## 4. Success criteria

| # | Criterion | Measure |
|---|---|---|
| S1 | The day view is answerable at a glance | Alex/Sam can state today's household commitments in <5s from 8ft away |
| S2 | Work noise is suppressed | Alex's 5 daily meetings occupy exactly one collapsed row |
| S3 | Adding an event is faster than the phone | "add soccer practice thursday at 4" → event created, <8s end-to-end |
| S4 | Dedup works | Zero visible duplicates across ≥2 connected sources over a 30-day window |
| S5 | Minor safety holds | Riley's session never receives an `adults` event in any API response |
| S6 | Cost stays trivial | < $10/mo AWS + Anthropic combined at family volume |

---

## 5. Architecture — hybrid (AWS control plane, local display)

```
┌───────────────────────── AWS  us-east-1 ─────────────────────────┐
│                                                                                       │
│  CloudFront ──► S3 (PWA bundle)                                                        │
│                                                                                       │
│  API Gateway (HTTP API)                                                                │
│      ├─► Lambda: api        (FastAPI + Mangum)   ── CRUD, agenda queries              │
│      ├─► Lambda: agent      (Anthropic tool loop) ── NL in, tool calls out            │
│      ├─► Lambda: sync       (EventBridge, 15 min) ── pull adapters, normalize         │
│      ├─► Lambda: classify   (invoked on ingest)   ── tier assignment                  │
│      └─► Lambda: sms        (SNS inbound)         ── text-in channel                  │
│                                                                                       │
│  DynamoDB `airhead` (single table)   Cognito user pool (3 users)                        │
│  SSM Parameter Store (OAuth refresh tokens, Anthropic key — SecureString)              │
└───────────────────────────────────────────────────────────────────────────────────────┘
                                        │ HTTPS
┌───────────────────────────────────────┴──────────── Kitchen (HP Spectre 16 → Pi) ─────┐
│  Debian minimal + Chromium kiosk  →  Airhead PWA                                        │
│  Service worker + IndexedDB  ── read-only cache; screen survives WAN outage            │
│  Local daemon: push-to-talk mic capture, screen wake/sleep, watchdog, health beacon    │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

**Why hybrid:** phones need the API from anywhere (so it can't be LAN-only), but a kitchen display
that goes blank when Comcast blips is worse than useless. The PWA caches the next 14 days; writes
queue in IndexedDB and replay on reconnect (last-write-wins, server timestamp authoritative).

**Pi migration path:** the kiosk is a browser pointed at a URL. Moving to a Pi is reimaging one
device. Moving the *control plane* to a Pi later means running the same FastAPI app under uvicorn
against SQLite/Postgres — hence the repository-pattern data layer in §7.

### Terraform

Follows existing conventions (see `kswiger-dev-domain` memory):
- State: an S3 bucket + DynamoDB lock table you supply via `backend.hcl` (gitignored).
- `project = "airhead"` prefixes every resource name and must never change.
- DNS: served from a subdomain of a domain you already own. The hosted zone is looked up
  with `data "aws_route53_zone"`, never created — see the two-zone trap in the README.
- `terraform fmt && terraform validate` on every `.tf` change. No console-created resources.

---

## 6. Core concepts

### 6.1 Relevance tiers

Assigned at ingest by the classifier, overridable by any adult. This is the whole product.

| Tier | Name | Meaning | Kitchen default view |
|------|------|---------|----------------------|
| `T1` | HOUSEHOLD | Affects someone else's plans — pickups, travel, dinners, appointments requiring a driver | Full row, title + time + who |
| `T2` | PERSONAL | Real commitment, doesn't obligate anyone else — gym, haircut, a friend's visit | Full row, dimmed |
| `T3` | BUSY | Work meetings, focus blocks, anything that only means "unavailable" | Collapsed into one band per person per day: `Alex ███ busy 9:00–15:00 (5)` — tap to expand |

**Classification rules (deterministic first, model second):**
1. Source-level default — a calendar connected as "work" defaults every event to `T3`.
2. Attendee heuristics — >3 attendees, or any external domain → `T3`.
3. Keyword/pattern table — "pickup", "dropoff", "dentist", "flight", "practice", "game" → `T1`.
4. Anything unresolved goes to the model (§9.3) with title, time, duration, attendee count,
   source label, and the household roster.
5. `tierSource` is stored as `auto` or `human`. **A re-sync never overwrites `human`.** This is the
   single most important invariant in the ingest path — get it wrong and every override evaporates
   at the next poll.

### 6.2 Visibility

`visibility` ∈ `all` | `adults`. Enforced **at the query layer in the API Lambda**, before any data
reaches the agent or the wire. The model is never trusted to redact — a prompt-injected calendar
title must not be able to talk its way into an adults-only event.

Minors additionally cannot: change another member's events, change visibility, delete events they
didn't create, or connect/disconnect calendar sources.

### 6.3 Merge groups

Duplicates are never deleted. Source records are immutable truth; a merge group is a view over them
with one designated canonical record. Unmerging is always possible and lossless.

---

## 7. Data model

DynamoDB single table `airhead`, PAY_PER_REQUEST, PITR on.

| Entity | PK | SK | Notes |
|---|---|---|---|
| Member | `HH#<hh>` | `MEMBER#<memberId>` | role: `adult` \| `minor`, color, displayName, cognitoSub |
| Event | `HH#<hh>` | `EVENT#<startUtc>#<eventId>` | range queries via `SK between` |
| Source | `HH#<hh>` | `SOURCE#<sourceId>` | kind, ownerMemberId, syncToken/ctag, defaultTier, label, lastSyncAt |
| MergeGroup | `HH#<hh>` | `MERGE#<groupId>` | memberEventIds[], canonicalEventId, confidence, decidedBy |
| ReviewItem | `HH#<hh>` | `REVIEW#<createdAt>#<id>` | ambiguous merges + low-confidence tiering |
| AgentTurn | `HH#<hh>` | `TURN#<ts>#<id>` | audit log, 90-day TTL |

**GSIs**
- `GSI1` — external identity, for idempotent upsert and dedup-by-origin.
  `GSI1PK = SRC#<sourceId>`, `GSI1SK = EXT#<externalId>`
- `GSI2` — per-member day slice. `GSI2PK = HH#<hh>#MEM#<memberId>`, `GSI2SK = <startUtc>`

**Event attributes**

```jsonc
{
  "eventId": "evt_...",
  "title": "Soccer practice",
  "startUtc": "2026-08-06T20:00:00Z",
  "endUtc":   "2026-08-06T21:30:00Z",
  "tz": "America/New_York",          // originating IANA zone, for DST-correct display
  "allDay": false,
  "rrule": "FREQ=WEEKLY;BYDAY=TH",   // RFC 5545, stored verbatim, expanded at read time
  "exdates": [],
  "ownerMemberId": "mem_shiloh",
  "involves": ["mem_shiloh", "mem_erica"],   // who this actually constrains
  "location": "Riverside Park Field 3",
  "tier": "T1",
  "tierSource": "auto",              // auto | human  — human is sticky forever
  "visibility": "all",
  "source": { "kind": "google", "sourceId": "src_...", "externalId": "...", "etag": "..." },
  "mergeGroupId": null,
  "contentHash": "sha256:...",       // change detection without field-by-field diffing
  "createdBy": "mem_kyle",
  "updatedAt": "2026-08-01T18:22:03Z"
}
```

**Recurrence:** store the RRULE, never the expansion. Expand on read inside a bounded window
(default 90 days forward, 30 back) using `python-dateutil`. Overrides to a single instance are
stored as their own event carrying `recurrenceParentId` + `recurrenceId`.

**Data access is behind a repository interface** (`EventRepo`, `SourceRepo`, …) with a DynamoDB
implementation and a SQLite implementation. The SQLite one is what pytest uses (hermetic,
in-memory) and what a future Pi-only deployment would use in production.

---

## 8. Calendar source adapters

One protocol, four implementations. v1 is pull-only; `push` is declared now and raises
`NotImplementedError` so the v2 work is additive.

```python
class CalendarSource(Protocol):
    kind: Literal["google", "caldav", "graph", "ics"]

    def authorize(self, config: SourceConfig) -> Credentials: ...
    def list_calendars(self, creds: Credentials) -> list[CalendarRef]: ...
    def pull(self, creds: Credentials, cursor: SyncCursor | None) -> PullResult: ...
    # PullResult = (upserts: list[ExternalEvent], deletions: list[str], cursor: SyncCursor)

    # v2
    def push(self, creds: Credentials, event: Event) -> ExternalRef: ...
    def remove(self, creds: Credentials, ref: ExternalRef) -> None: ...
```

| Adapter | Transport | Incremental mechanism | Auth | Notes |
|---|---|---|---|---|
| `google` | Calendar API v3 | `syncToken` on `events.list` | OAuth 2.0, refresh token in SSM | See risk R1 |
| `caldav` | CalDAV (iCloud) | `sync-collection` REPORT, ctag/etag | App-specific password | 2FA account required |
| `graph` | Microsoft Graph | `/me/calendarView/delta` | OAuth 2.0 (Entra) | Work account may block third-party apps |
| `ics` | HTTPS GET | `ETag` / `Last-Modified` | none (secret URL) | Universal fallback; 8–24h publish lag upstream |

**Normalization** happens once, in the adapter, into `ExternalEvent`. Downstream code never sees a
provider-specific shape. All-day events normalize to a floating date + `allDay: true`, not a
midnight UTC instant (the classic off-by-one-day bug).

**Cadence:** EventBridge Scheduler → `sync` Lambda every 15 min, all sources. Google push
notifications (`watch` channels) are a v2 optimization, not a v1 dependency.

---

## 9. Dedup and merge

### 9.1 Blocking

Only compare events whose start times fall within ±45 min of each other and whose date ranges
overlap. Cheap DynamoDB range query; keeps the comparison set to single digits.

### 9.2 Scoring

| Signal | Weight | Method |
|---|---|---|
| Title similarity | 0.40 | Normalized token-set ratio after stripping punctuation, emoji, and organizer prefixes |
| Start-time delta | 0.25 | 1.0 at exact match, linear decay to 0 at 45 min |
| Duration delta | 0.10 | ratio |
| Location similarity | 0.15 | token-set ratio; empty on either side scores 0.5 (neutral) |
| Attendee/member overlap | 0.10 | Jaccard over resolved household members |

- `score ≥ 0.85` → auto-merge. Canonical = the record from the highest-priority source (household
  native > Google > Graph > CalDAV > ICS).
- `0.60 ≤ score < 0.85` → ReviewItem. The agent surfaces it conversationally ("Is Thursday's
  'Soccer' the same as 'Riley soccer practice'?"); one adult tap resolves it.
- `< 0.60` → distinct.

Human merge/unmerge decisions are recorded as **pairwise rules** keyed by normalized title +
source pair, so the same recurring pair never gets asked about twice.

### 9.3 Model use in ingest

The classifier call handles tier assignment for events the deterministic rules can't resolve, and
adjudicates merge candidates in the ambiguous band. Structured output, strict schema, no free text.

---

## 10. Agent

**Model:** `claude-opus-5` via the official Python SDK (`anthropic`), using the beta tool runner
(`client.beta.messages.tool_runner`) rather than a hand-rolled loop.

- Thinking is on by default on Opus 5 — `max_tokens` caps thinking *plus* response text, so size it
  generously (16k non-streaming, 64k if we stream to the screen).
- `output_config={"effort": "medium"}` for the conversational agent; sweep low/medium/high against
  real transcripts before locking it in. Opus 5 is unusually strong at low/medium — that's the
  primary cost and latency lever.
- Prompt caching on the system prompt + tool definitions + household roster (Opus 5's minimum
  cacheable prefix is 512 tokens, so even our modest preamble caches). The roster and tool list must
  be serialized deterministically — sorted keys, stable order — or the cache silently never hits.
- No timestamps interpolated into the system prompt. "Now" arrives as a user-turn fact, after the
  cache breakpoint.
- **Cost option (Alex's call, not assumed):** the ingest classifier in §9.3 is a narrow,
  high-volume, low-judgment task. Running it on `claude-haiku-4-5` ($1/$5 per MTok vs $5/$25) would
  cut the largest token line item by ~80%. Spec'd as Opus 5; flip it if the eval holds.

### 10.1 Tool surface

Every tool call carries a server-injected `actor_member_id`. The model cannot set or spoof it.

| Tool | Confirm? | Notes |
|---|---|---|
| `get_agenda(start, end, member?, min_tier?)` | no | Visibility filter applied server-side before return |
| `find_conflicts(start, end)` | no | Overlapping T1/T2 across members |
| `create_event(...)` | no | Defaults `ownerMemberId` to actor |
| `update_event(event_id, patch)` | yes if not owner | |
| `delete_event(event_id)` | **always** | Soft delete, 30-day tombstone |
| `set_tier(event_id, tier)` | no | Sets `tierSource: human` |
| `set_visibility(event_id, v)` | adults only | Rejected for minor actors at the API layer |
| `merge_events(ids)` / `unmerge(group_id)` | no | |
| `list_members()` | no | |

Confirmation is a **harness gate, not a prompt instruction** — the tool's `run` function returns a
pending-confirmation result and the UI renders a confirm affordance. The model can't talk its way
past it.

### 10.2 Input channels

| Channel | v1 | Notes |
|---|---|---|
| Touchscreen chat | ✅ | Primary. Large tap targets, no keyboard gymnastics |
| Touchscreen voice | ✅ | **Push-to-talk only, no wake word.** See §12 |
| SMS text-in | ✅ | SNS inbound; see risk R3 |
| Phone PWA chat | v1.5 | Same UI, authenticated per member; drops out of scope if the timeline slips |

---

## 11. Kitchen display UX

**Default view — today, tiered:**

```
SAT  Aug 2                                          72° ☀
─────────────────────────────────────────────────────────
  Alex     ███████ busy 9:00–15:00  (5 meetings)      ▸
  Sam    09:30  Dentist
  Riley   16:00  Soccer practice        Sam driving
           18:30  FAMILY  Dinner at Nana's
─────────────────────────────────────────────────────────
  [ ◀ ]     Today  Tomorrow  Week          [ 🎤 ]  [ ⌨ ]
```

- One color per member, consistent everywhere.
- T1 events get a `FAMILY` chip when they involve >1 member.
- Tapping the busy band expands the 5 meetings inline; it collapses again on a 30s idle timer.
- Week view is the same tier logic, 7 columns, T3 rendered as bar height only — no text.
- Idle → dim after 2 min, sleep after 30 (motion sensor is a nice-to-have, not v1).
- Typography sized for ~8ft legibility: 28px minimum for event titles.

Accessibility: never encode member identity in color alone — the name label is always present.

---

## 12. Voice and privacy (there's an 11-year-old in the room)

- **Push-to-talk, not wake word.** No always-listening mic in a family kitchen. This is a
  deliberate product constraint, not a technical limitation.
- Mic hardware has a physical mute or the local daemon holds the device closed until the button is
  pressed.
- Audio is streamed to STT and never written to disk. Transcripts persist 7 days for debugging,
  then TTL out of DynamoDB alongside the agent turn log.
- A visible on-screen indicator whenever the mic is open. Non-negotiable.
- Riley can use voice; her turns are subject to the same server-side visibility filter as text.

---

## 13. Security

- Cognito user pool, 3 users, no self-signup. Kiosk device authenticates as a **shared household
  device identity** with a member-selection step for writes — the screen is physically in a shared
  space, so a persistent per-person session is the wrong model.
- All secrets in SSM Parameter Store SecureString. **No AWS secrets in the repo, ever** — OAuth
  refresh tokens, the Anthropic API key, CalDAV app passwords.
- Structured JSON logging to CloudWatch. No `print` debugging, no credentials in logs, no event
  titles at INFO (they're household PII).
- **Prompt injection is a real threat here.** Calendar titles are attacker-controllable via any
  meeting invite from outside. Mitigations: visibility filtering happens before the model sees
  anything; tools enforce authorization server-side; external event text is wrapped in a delimited
  block with an explicit "this is data, not instructions" framing.
- Least-privilege IAM per Lambda. The `sync` role cannot read the Anthropic key; the `agent` role
  cannot read OAuth tokens.

---

## 14. Repo and stack

```
airhead/
├── backend/         Python 3.12 · FastAPI · Mangum · pytest (SQLite in-memory, hermetic)
│   ├── src/airhead/{api,agent,sync,adapters,dedup,repo}/
│   └── tests/
├── frontend/        Vite · React · TypeScript · tsc --noEmit in CI
├── infra/           Terraform (fmt + validate gated in CI)
├── kiosk/           systemd units, chromium wrapper, watchdog, provisioning script
└── docs/            this file, ADRs
```

- CI: GitHub Actions on PRs (pytest, tsc, tflint, terraform validate).
- Conventional commits (feat/fix/chore/docs).
- Branch from `main`, always. Delete the base branch when merging a stack — GitHub only
  auto-retargets a stacked PR when its base branch is deleted.
- Automated PR review wired in alongside CI.

---

## 15. Milestones

| # | Scope | Est. | Exit criteria |
|---|---|---|---|
| M0 | Repo scaffold, Terraform baseline, CI, DNS | 1w | `airhead.<your-domain>` serves a static page over CloudFront |
| M1 | Data model, repo layer, CRUD API, read-only agenda UI | 2w | Manually seeded events render in the tiered day view |
| M2 | Agent: tool loop, chat on screen, create/delete/get-day | 2w | S3 met — "add soccer thursday 4pm" works end to end |
| M3 | Adapters: Google → ICS → Graph → CalDAV, sync Lambda | 3w | Three sources syncing on a 15-min cadence, no dupes visible yet |
| M4 | Dedup/merge + tier classifier + review queue | 2w | S4 met over a 7-day soak |
| M5 | Kiosk: Spectre reflash, kiosk mode, mount, power mgmt | 1w | Screen lives on the wall, survives a reboot and a WAN outage |
| M6 | Voice (push-to-talk) + SMS channel | 2w | S1–S6 all met |

M5 has a hardware dependency (chassis work on a damaged Spectre) that's independent of everything
else — start the physical build in parallel with M1.

---

## 16. Cost

| Item | Est./mo |
|---|---|
| Lambda + API Gateway + DynamoDB (family volume) | ~$1 |
| CloudFront + S3 | ~$0.50 |
| Route 53 hosted zone (existing, shared) | $0 marginal |
| Anthropic — agent turns (~20/day, cached prefix) | $3–6 |
| Anthropic — ingest classifier (Opus 5; Haiku option ≈ –80%) | $2–4 |
| SMS (toll-free, once out of sandbox) | ~$1 |
| **Total** | **$7–13** |

No NAT gateway, no VPC endpoints. (VPC endpoints are an expensive way to discover you never needed a VPC — Airhead's Lambdas
have no VPC attachment and must not acquire one.)

---

## 17. Risks and open questions

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| **R1** | **Google OAuth refresh tokens expire every 7 days while the app is in "Testing" publishing status.** Calendar scopes are *sensitive*, so moving to Production requires Google verification (weeks, and a privacy policy + demo video). | Google sync silently dies weekly | Decide early: (a) submit for verification at M0 so it clears by M3, (b) accept weekly re-auth with a nag on the kitchen screen, or (c) use Google's secret ICS address for v1 — read-only, no OAuth, 8–24h lag. **Needs a decision before M3.** |
| R2 | Apple CalDAV requires an app-specific password and an Apple ID with 2FA; iCloud throttles aggressively | Apple sync flaky | Poll no more than every 15 min, honor ctag, exponential backoff |
| R3 | SMS: toll-free number registration ran ~2–3 weeks at carrier review in a prior project, and SNS SMS production access is a separate console-only case. Both must clear. | SMS channel slips | File both at M0. Treat SMS as M6-optional; screen chat + voice carry v1 |
| R4 | Outlook work tenant may forbid third-party app consent | No Outlook ingest | Fall back to the published ICS feed from Outlook |
| R5 | Spectre chassis is damaged; mounting requires custom work | M5 slips | Start physically in parallel with M1; a cheap tablet in kiosk mode is the fallback display |
| R6 | Tier classification gets it wrong and hides something that mattered | Trust collapse — this is the product-killing failure | Bias toward T1 when uncertain. Never *hide* T3; always collapse-with-count so nothing is invisible. One-tap "always treat this as household" that writes a sticky rule |
| R7 | Prompt injection via an external meeting invite | Data exposure | See §13 |

**Open questions for Alex**
1. R1 — which Google path?
2. Does Alex have a Google Workspace domain? (If yes, R1 collapses to service-account + domain-wide
   delegation and the whole problem disappears.)
3. Ingest classifier on Haiku 4.5 or Opus 5?
4. Should Riley be able to create events for other people, or only for herself?
5. Mic hardware: USB array mic, or whatever's in the Spectre?
