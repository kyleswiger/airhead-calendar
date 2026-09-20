# Routines contract

"Routines" are the things a household does *every so often* and otherwise keeps track of in
somebody's head: change the cabin air filter, get a haircut, clean the gutters, put the holiday
lights up. Airhead records **when it was last done**, works out **when it is next due**, and
puts the due date on the kitchen calendar so nobody has to remember.

Frozen the same way `M1-CONTRACT.md` was: the API, the agent tools, the stores and the display
are built against this document in parallel. Changes here are a PR that touches every side.

Base path: `/api`. `camelCase` on the wire, `snake_case` in Python. Dates are bare
`YYYY-MM-DD` household-local dates — a routine is done "on a day", never at an instant.

## Ground rules

1. **Deterministic first, model second** (PRD §6.1 applies here too). The interval for a
   routine is resolved in this order and the winner is recorded in `intervalSource`:
   1. `human` — the person said how often. Sticky: nothing below may ever overwrite it.
   2. `observed` — three or more completions exist; the median gap between them is the
      household's real cadence, which beats any book value.
   3. `catalog` — the name matches `airhead.routines.catalog` (a curated table of ~100
      common items with sourced intervals).
   4. `estimated` — a model estimate, stored with its rationale and confidence so the
      display can say "est." and the person can correct it with one tap.
   5. Unknown. The routine exists, `intervalDays` is `null`, and it is `unscheduled`
      until someone answers. Never invent a number silently.
2. **The due date is a real calendar event.** A routine with a due date owns exactly one
   native all-day `Event` (`routineId` set, tier/visibility/involves copied from the
   routine). It renders on the agenda like any other all-day row, is visible to
   `get_agenda`, obeys the visibility filter at the query layer, and needs no new row kind.
3. **Overdue never disappears.** An overdue due-event is rolled forward to *today* by
   `reproject` (run on `GET /api/routines` and `GET /api/agenda`), so it stays on the
   kitchen screen until it is completed, snoozed or paused. Same rule as T3 collapse: nothing
   that mattered once may silently vanish off the display.
4. **Completing is cheap and undoable.** One tap / one sentence records a completion,
   recomputes the interval (unless `human`), and re-projects the due event. A completion
   can be deleted, which rolls all of that back. Mis-taps on a kitchen touchscreen are normal.
5. **Visibility is the query layer's job**, exactly as for events. A minor's session never
   receives an `adults` routine or its due event.

## Domain (`airhead.domain`)

```python
class IntervalSource(StrEnum): HUMAN = "human"; OBSERVED = "observed"; CATALOG = "catalog"; ESTIMATED = "estimated"
class Anchor(StrEnum):         ELAPSED = "elapsed"; CALENDAR = "calendar"

@dataclass Routine:
    routine_id: str                 # "rtn_<hex>"
    household_id: str
    name: str                       # the household's own words: "Cabin air filter — EV6"
    owner_member_id: str
    category: str = "other"         # catalog category, or free text
    interval_days: int | None = None
    interval_source: IntervalSource = IntervalSource.ESTIMATED   # meaningless while interval_days is None
    interval_note: str | None = None      # one line of "why": catalog note or model rationale
    interval_confidence: float | None = None   # 0..1, estimated only
    anchor: Anchor = Anchor.ELAPSED       # CALENDAR = same month/day every year (holiday lights)
    involves: list[str] = []
    tier: Tier = Tier.PERSONAL
    visibility: Visibility = Visibility.ALL
    catalog_key: str | None = None
    last_done_on: date | None = None      # denormalised from completions
    due_on: date | None = None            # projected, or an explicit snooze
    due_event_id: str | None = None       # the one calendar event this routine owns
    paused: bool = False
    created_by: str | None = None
    updated_at: datetime | None = None
    deleted_at: datetime | None = None    # soft delete, like events

@dataclass Completion:
    completion_id: str              # "cmp_<hex>"
    household_id: str
    routine_id: str
    done_on: date
    by_member_id: str
    note: str | None = None
    created_at: datetime | None = None
```

`Event` gains one optional field, `routine_id: str | None`, serialised as `routineId` on the
wire (additive; the display shows a `DUE` chip when present).

## Projection (`airhead.routines.service`)

```
next_due(routine, today) -> date | None
    anchor ELAPSED : last_done_on + interval_days            (None if either is missing)
    anchor CALENDAR: next occurrence of last_done_on's month/day strictly after last_done_on
status(routine, today) -> "paused" | "unscheduled" | "overdue" | "due_soon" | "ok"
    due_soon = due within 7 days (inclusive)
reproject(routine, events, *, today) -> Routine
    idempotent. Ensures the due event exists on max(due_on, today) with the routine's
    current title/tier/visibility/involves; soft-deletes the event when paused/unscheduled.
complete(routine, completions, events, *, done_on, by, note, today) -> Routine
    appends a Completion, updates last_done_on, recomputes interval (observed rule, never
    over `human`), clears any snooze, re-projects.
resolve_interval(name, stated=None, completions=[]) -> Resolution
    the ground-rule-1 ladder, *without* the model step. The model step is the caller's:
    the agent estimates in-loop; the HTTP API exposes POST /api/routines/estimate.
```

## Storage

| Entity | SQLite | DynamoDB PK / SK |
|---|---|---|
| Routine | `routines` | `HH#<hh>` / `ROUTINE#<routineId>` |
| Completion | `routine_completions` | `HH#<hh>` / `RCOMP#<routineId>#<doneOn>#<completionId>` |

`RoutineRepo` protocol (`airhead.repo.base`):

```python
get(household_id, routine_id) -> Routine | None          # tombstones included
put(routine) -> Routine                                  # stamps updated_at
delete(household_id, routine_id, *, at) -> Routine | None  # soft
list(household_id, *, include_deleted=False) -> list[Routine]   # ordered by routine_id
add_completion(completion) -> Completion
list_completions(household_id, routine_id) -> list[Completion]  # ordered by done_on, then id
delete_completion(household_id, routine_id, completion_id) -> bool
```

Contract-tested against both backends, like `EventRepo`.

## HTTP

### `GET /api/routines`

Runs `reproject` for every live routine first, then returns them ordered: overdue (most
overdue first) → due_soon → ok (soonest first) → unscheduled → paused.

```jsonc
{
  "routines": [
    {
      "routineId": "rtn_…",
      "name": "Cabin air filter — EV6",
      "category": "vehicle",
      "ownerMemberId": "mem_alex",
      "memberIds": ["mem_alex"],           // owner + involves, roster order (same rule as events)
      "tier": "T2",
      "visibility": "all",
      "intervalDays": 365,
      "intervalSource": "catalog",        // human | observed | catalog | estimated | null
      "intervalNote": "Kia schedules the cabin filter at 12 months / 15,000 mi.",
      "intervalConfidence": null,
      "anchor": "elapsed",
      "catalogKey": "cabin_air_filter",
      "lastDoneOn": "2026-09-20",
      "dueOn": "2027-09-20",
      "dueEventId": "evt_…",
      "status": "ok",                      // paused | unscheduled | overdue | due_soon | ok
      "daysUntilDue": 365,                 // negative when overdue, null when no dueOn
      "completionCount": 1,
      "paused": false
    }
  ]
}
```

### `POST /api/routines` → `201` routine

```jsonc
{
  "name": "Haircut",                     // required
  "category": "personal_care",           // optional; catalog fills it on a match
  "intervalDays": 35,                    // optional; present ⇒ intervalSource "human"
  "anchor": "elapsed",                   // optional
  "ownerMemberId": "mem_alex",           // optional, defaults to actor
  "involves": [],
  "tier": "T2",
  "visibility": "all",                   // adults only, as for events
  "lastDoneOn": "2026-09-20",            // optional; present ⇒ a first completion is recorded
  "dueOn": "2026-10-25",                 // optional explicit first due date
  "intervalNote": "…", "intervalConfidence": 0.7, "intervalSource": "estimated"
                                         // optional: a client that already ran /estimate passes it through
}
```
With no `intervalDays`, the server tries the catalog. It **does not** call the model — that is
the explicit `/estimate` route below. A routine with no interval is created `unscheduled`.

### `POST /api/routines/estimate` → `200`

```jsonc
// request
{ "name": "Cabin air filter 2023 Kia EV6", "context": "we drive ~10k miles a year" }
// response
{
  "intervalDays": 365, "rangeDays": [180, 365], "confidence": 0.8,
  "source": "catalog",                   // catalog | estimated
  "rationale": "Kia's EV6 schedule replaces the cabin filter every 12 months or 15,000 mi.",
  "usageDependent": true,                // mileage / usage changes it
  "followUpQuestion": null,              // set when the model needs one fact to do better
  "catalogKey": "cabin_air_filter", "category": "vehicle", "anchor": "elapsed"
}
```
Catalog hit ⇒ no model call. Otherwise one structured model call (`airhead.routines.estimate`).
Adults and minors alike; it writes nothing.

### `GET /api/routines/{id}` · `PATCH /api/routines/{id}` · `DELETE /api/routines/{id}`

PATCH body: any of `name, category, intervalDays, anchor, involves, tier, visibility, dueOn,
paused, intervalNote`. `intervalDays` through PATCH stamps `intervalSource: "human"`. `dueOn`
is a snooze: it is kept until the next completion. `404` for invisible/tombstoned, `403` for a
minor editing a routine they do not own. DELETE also soft-deletes the due event.

### `POST /api/routines/{id}/complete` → `200` routine

```jsonc
{ "doneOn": "2026-09-20", "note": "Bosch filter, 41,200 mi" }   // both optional; doneOn defaults to today
```
Any member who can see the routine may complete it. `doneOn` may not be in the future
(`400 future_completion`).

### `GET /api/routines/{id}/completions`

```jsonc
{ "completions": [ { "completionId": "cmp_…", "doneOn": "2026-09-20", "byMemberId": "mem_alex", "note": "…" } ] }
```

### `DELETE /api/routines/{id}/completions/{completionId}` → `204`

Undo. Recomputes `lastDoneOn`, the observed interval and the due event.

### Agenda

`GET /api/agenda` runs `reproject` before reading, and `event` rows carry `"routineId"` when
the row is a routine's due event.

## Agent tools

| Tool | Confirm? | Notes |
|---|---|---|
| `list_routines()` | no | Same rows as `GET /api/routines`, fenced as calendar data |
| `log_done(routine_id, done_on?, note?)` | no | "I changed the cabin filter this morning" |
| `create_routine(name, interval_days?, interval_stated_by_person, interval_note?, interval_confidence?, last_done_on?, due_on?, anchor?, category?, owner_member_id?, involves?, tier?, visibility?)` | no | Catalog beats a model estimate; a person's number beats both. The model estimates in-loop when the catalog misses and the person did not say — it must set `interval_stated_by_person=false` and give a one-line note, and should say "about every N months, tell me if that's wrong" |
| `update_routine(routine_id, …)` | yes if not owner | Same fields as PATCH, incl. `due_on` for "remind me next month instead" |
| `delete_routine(routine_id)` | always | Soft |
| `undo_completion(routine_id, completion_id)` | no | "I didn't actually do that" |

## Display

- A **Routines** panel (button on the nav bar): the ordered list above, each row with a status
  chip (`OVERDUE` / `DUE SOON` / `est.`), "last done N days ago", and one large **Done today**
  button. An **Add** form with name + optional "every N days" and a **Suggest** button that
  calls `/estimate` and fills the interval with its rationale.
- Event rows with `routineId` get a `DUE` chip and tap-to-complete.
- Nothing in the display computes intervals or due dates. The server is the only clock.
