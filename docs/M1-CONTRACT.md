# M1 wire contract

Frozen for M1 so the API, the agenda assembler, and the display can be built against
each other without either side guessing. Changes here are a PR that touches both sides.

Base path: `/api`. All JSON, `camelCase` on the wire, `snake_case` in Python.

## Ground rules

- **Instants are UTC ISO-8601 with `Z`.** Anything the display positions on a day grid is
  sent *additionally* as a floating local string, already converted server-side. The kiosk
  does no timezone math — the household timezone is a server concern and a Pi in a kitchen
  is exactly where a browser-side DST bug would go unnoticed for six months.
- **All-day events carry `"allDay": true`, and their `startLocal` / `endLocal` are bare
  dates** (`"2026-08-10"`), never midnight instants. **`endLocal` is inclusive** — the last
  day the event covers, so a one-day event has `startLocal == endLocal`. An exclusive
  next-midnight end on the wire is the classic off-by-one-day bug; we do not send one, and a
  client must not "correct" for one. Consumers branch on `allDay` rather than assuming a
  single format.
- **All-day events are stored floating**, i.e. the date is read off the clock face of
  `startUtc` without a zone conversion. Adapters must follow suit: converting an all-day
  event *through* a timezone is what lands it a day off, and it does so silently.
- **The caller never asks for a visibility level.** The server derives it from the
  authenticated member. A minor's session cannot request `adults` events by any parameter.
- Unknown fields on a response are additive; the display must ignore what it doesn't know.

## `GET /api/agenda`

| Param | Required | Notes |
|---|---|---|
| `start` | yes | `YYYY-MM-DD`, inclusive, household-local |
| `end` | yes | `YYYY-MM-DD`, inclusive. Max span 31 days; over that → `400` |
| `minTier` | no | `T1` \| `T2` \| `T3`, default `T3` (everything) |
| `memberId` | no | repeatable; default every member |

```jsonc
{
  "range": { "start": "2026-08-04", "end": "2026-08-10", "tz": "America/New_York" },
  "members": [
    { "memberId": "mem_alex", "displayName": "Alex", "role": "adult", "color": "#7aa2f7" }
  ],
  "days": [
    {
      "date": "2026-08-04",
      "rows": [
        {
          "kind": "event",
          "eventId": "evt_01J...",
          "title": "Soccer practice",
          "startLocal": "2026-08-04T16:00:00",   // floating, household tz
          "endLocal": "2026-08-04T17:30:00",
          "startUtc": "2026-08-04T20:00:00Z",
          "allDay": false,
          "tier": "T1",
          "tierSource": "auto",
          "ownerMemberId": "mem_riley",
          "memberIds": ["mem_riley", "mem_sam"],  // who this actually constrains
          "location": "Riverside Park Field 3",
          "visibility": "all",
          "isFamily": true,                       // memberIds.length > 1 && tier == T1
          "occurrenceId": "evt_01J...@2026-08-04T20:00:00Z"  // set on expanded instances
        },
        {
          "kind": "busy",                         // T3 collapse. NEVER omitted, NEVER hidden.
          "memberId": "mem_alex",
          "startLocal": "2026-08-04T09:00:00",
          "endLocal": "2026-08-04T15:00:00",
          "count": 5,
          "eventIds": ["evt_...", "evt_..."]      // for the tap-to-expand inline list
        }
      ]
    }
  ]
}
```

**Row ordering within a day:** `busy` bands first (one per member, ordered by the member
order in `members`), then `event` rows by `startLocal`, all-day events ahead of timed ones.

**The T3 rule is structural, not cosmetic.** A day with work meetings always emits a `busy`
row carrying a truthful `count`. There is no response shape in which a T3 event silently
disappears — hiding something that mattered once ends trust in the whole display permanently.
A `busy` band spans the first start to the last end of that member's T3 events that day; a
member with zero T3 events that day gets no band at all (absence of a band means absence of
work, which is information).

## `GET /api/members`

```jsonc
{ "members": [ { "memberId": "mem_alex", "displayName": "Alex", "role": "adult", "color": "#7aa2f7" } ] }
```

## Event CRUD

- `POST /api/events` → `201`, body is a single `event` row object (same shape as above).
- `GET /api/events/{eventId}` → `200` / `404`. A tombstoned or invisible event returns
  `404`, never `403` — a distinguishable "exists but you can't see it" is itself a leak.
- `PATCH /api/events/{eventId}` → `200`. Partial. Setting `tier` through this route stamps
  `tierSource: "human"`, which no later sync may overwrite. **`POST` with an explicit `tier`
  stamps `human` too** — a tier a person typed is a human tier, and if it were recorded as
  `auto` the first sync would quietly "correct" it back.
- `DELETE /api/events/{eventId}` → `204`. Soft delete.

Request and response deliberately differ on one field: a body sends **`involves`** (who else
this constrains), a row returns **`memberIds`** (owner + involves, deduped, ordered to match
the roster order in `members`). The response field is the resolved set, so a client can render
it directly; the request field is what a human actually knows.

Create/patch body (all optional on PATCH):

```jsonc
{
  "title": "Soccer practice",
  "startLocal": "2026-08-04T16:00:00",  // or "date" + allDay
  "endLocal": "2026-08-04T17:30:00",
  "allDay": false,
  "tz": "America/New_York",             // defaults to household tz
  "rrule": "FREQ=WEEKLY;BYDAY=TH",
  "ownerMemberId": "mem_riley",
  "involves": ["mem_riley", "mem_sam"],
  "location": "Riverside Park Field 3",
  "tier": "T1",
  "visibility": "all"
}
```

## Errors

```jsonc
{ "error": { "code": "range_too_large", "message": "Agenda span may not exceed 31 days." } }
```

`400 bad_request` · `403 forbidden` (minor attempting an adult-only mutation) ·
`404 not_found` · `409 conflict` · `422 validation_error`.

## Auth in M1

M1 ships behind a header shim: `X-Airhead-Member` names the acting member and the API
resolves the roster from the repo. It is a **placeholder for the Cognito authorizer** in a
later milestone and is the only thing that changes when Cognito lands — every route already
reads its actor from one dependency, and authorization decisions are already server-side.
