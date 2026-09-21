/**
 * The fixture-mode stand-in for the routines API.
 *
 * This module *is* the fake server, which is the one place in the frontend
 * allowed to project a due date: the display proper still only reads what it
 * is handed (contract § Display - "the server is the only clock"). It keeps a
 * mutable in-memory sample so Done / Undo / Add / Pause actually move things
 * on the demo screen, and it emits the due events so the agenda shows the DUE
 * chip. Nothing persists; a reload resets the sample.
 */

import routinesFixture from "../fixtures/routines.sample.json";
import type {
  AllDayEventRow,
  Completion,
  CreateRoutineBody,
  EstimateResponse,
  Routine,
  RoutinePatch,
  RoutineStatus,
} from "../types";
import { addDays, daysBetween, todayIsoDate } from "./format";
import { isRecord, parseCompletion, parseRoutine } from "./parse";
import { compareRoutines } from "./routines";

interface Stored {
  routine: Routine;
  completions: Completion[];
}

let seq = 0;
function nextId(prefix: string): string {
  seq += 1;
  return `${prefix}_fx${seq}`;
}

/** Days the sample is shifted by so its `asOf` lands on today. */
function fixtureDelta(): number {
  const raw = routinesFixture as unknown;
  const asOf = isRecord(raw) && typeof raw["asOf"] === "string" ? raw["asOf"] : null;
  return asOf === null ? 0 : daysBetween(asOf, todayIsoDate());
}

function shift(date: string | null, delta: number): string | null {
  return date === null ? null : addDays(date, delta);
}

function load(): Map<string, Stored> {
  const out = new Map<string, Stored>();
  const raw = routinesFixture as unknown;
  const list = isRecord(raw) ? raw["routines"] : undefined;
  if (!Array.isArray(list)) return out;
  const delta = fixtureDelta();
  for (const entry of list) {
    const routine = parseRoutine(entry);
    if (routine === null) continue;
    const rawCompletions = isRecord(entry) ? entry["completions"] : undefined;
    const completions = Array.isArray(rawCompletions)
      ? rawCompletions
          .map(parseCompletion)
          .filter((c): c is Completion => c !== null)
          .map((c) => ({ ...c, doneOn: addDays(c.doneOn, delta) }))
      : [];
    out.set(routine.routineId, {
      routine: { ...routine, lastDoneOn: shift(routine.lastDoneOn, delta) },
      completions,
    });
  }
  return out;
}

const STORE = load();

// --- projection (mirrors airhead.routines.service, for the fake server only) --

function nextDue(r: Routine): string | null {
  if (r.lastDoneOn === null || r.intervalDays === null) return null;
  if (r.anchor === "calendar") {
    // Same month/day, next year. Good enough for a sample - leap days are not.
    return addDays(r.lastDoneOn, 365);
  }
  return addDays(r.lastDoneOn, r.intervalDays);
}

function statusOf(r: Routine, dueOn: string | null, today: string): RoutineStatus {
  if (r.paused) return "paused";
  if (dueOn === null) return "unscheduled";
  const d = daysBetween(today, dueOn);
  if (d < 0) return "overdue";
  if (d <= 7) return "due_soon";
  return "ok";
}

/** `reproject`: recompute the derived fields from the stored facts. */
function reproject(stored: Stored, today: string): Routine {
  const r = stored.routine;
  const lastDoneOn =
    stored.completions.length === 0
      ? null
      : stored.completions.reduce((max, c) => (c.doneOn > max ? c.doneOn : max), "");
  const base: Routine = {
    ...r,
    lastDoneOn: lastDoneOn === "" ? null : lastDoneOn,
    completionCount: stored.completions.length,
  };
  // An explicit snooze (`dueOn` set by PATCH) wins until the next completion;
  // the fake server keeps the snooze in `routine.dueOn` and clears it on complete.
  const projected = r.dueOn !== null && r.dueOn > (nextDue(base) ?? "") ? r.dueOn : nextDue(base);
  const dueOn = base.paused ? null : projected;
  const status = statusOf(base, dueOn, today);
  const next: Routine = {
    ...base,
    dueOn,
    status,
    daysUntilDue: dueOn === null ? null : daysBetween(today, dueOn),
    dueEventId: dueOn === null ? null : `evt_due_${r.routineId}`,
  };
  stored.routine = { ...next, dueOn: r.dueOn };
  return next;
}

// --- public fake API --------------------------------------------------------

export function fixtureListRoutines(): Routine[] {
  const today = todayIsoDate();
  return [...STORE.values()].map((s) => reproject(s, today)).sort(compareRoutines);
}

export function fixtureGetRoutine(id: string): Routine | null {
  const stored = STORE.get(id);
  return stored === undefined ? null : reproject(stored, todayIsoDate());
}

export function fixtureCreateRoutine(body: CreateRoutineBody): Routine {
  const routineId = nextId("rtn");
  const owner = body.ownerMemberId ?? "mem_alex";
  const involves = body.involves ?? [];
  const intervalDays = body.intervalDays ?? null;
  const routine: Routine = {
    routineId,
    name: body.name,
    category: body.category ?? "other",
    ownerMemberId: owner,
    memberIds: [owner, ...involves.filter((m) => m !== owner)],
    tier: body.tier ?? "T2",
    visibility: body.visibility ?? "all",
    intervalDays,
    intervalSource: intervalDays === null ? null : (body.intervalSource ?? "human"),
    intervalNote: body.intervalNote ?? null,
    intervalConfidence: body.intervalConfidence ?? null,
    anchor: body.anchor ?? "elapsed",
    catalogKey: null,
    lastDoneOn: null,
    dueOn: body.dueOn ?? null,
    dueEventId: null,
    status: "unscheduled",
    daysUntilDue: null,
    completionCount: 0,
    paused: false,
  };
  const completions: Completion[] = [];
  if (body.lastDoneOn !== undefined) {
    completions.push({ completionId: nextId("cmp"), doneOn: body.lastDoneOn, byMemberId: owner });
  }
  const stored: Stored = { routine, completions };
  STORE.set(routineId, stored);
  return reproject(stored, todayIsoDate());
}

export function fixtureCompleteRoutine(
  id: string,
  doneOn: string | undefined,
  note: string | undefined,
  by: string,
): Routine | null {
  const stored = STORE.get(id);
  if (stored === undefined) return null;
  const completion: Completion = {
    completionId: nextId("cmp"),
    doneOn: doneOn ?? todayIsoDate(),
    byMemberId: by,
  };
  if (note !== undefined) completion.note = note;
  stored.completions.push(completion);
  stored.completions.sort((a, b) => a.doneOn.localeCompare(b.doneOn));
  // A completion clears any snooze.
  stored.routine = { ...stored.routine, dueOn: null };
  return reproject(stored, todayIsoDate());
}

export function fixtureListCompletions(id: string): Completion[] {
  return [...(STORE.get(id)?.completions ?? [])];
}

export function fixtureUndoCompletion(id: string, completionId: string): boolean {
  const stored = STORE.get(id);
  if (stored === undefined) return false;
  const before = stored.completions.length;
  stored.completions = stored.completions.filter((c) => c.completionId !== completionId);
  reproject(stored, todayIsoDate());
  return stored.completions.length < before;
}

export function fixturePatchRoutine(id: string, patch: RoutinePatch): Routine | null {
  const stored = STORE.get(id);
  if (stored === undefined) return null;
  const r = stored.routine;
  const next: Routine = { ...r };
  if (patch.name !== undefined) next.name = patch.name;
  if (patch.category !== undefined) next.category = patch.category;
  if (patch.intervalDays !== undefined) {
    next.intervalDays = patch.intervalDays;
    next.intervalSource = "human";
  }
  if (patch.anchor !== undefined) next.anchor = patch.anchor;
  if (patch.tier !== undefined) next.tier = patch.tier;
  if (patch.visibility !== undefined) next.visibility = patch.visibility;
  if (patch.dueOn !== undefined) next.dueOn = patch.dueOn;
  if (patch.paused !== undefined) next.paused = patch.paused;
  if (patch.intervalNote !== undefined) next.intervalNote = patch.intervalNote;
  if (patch.involves !== undefined) {
    next.memberIds = [r.ownerMemberId, ...patch.involves.filter((m) => m !== r.ownerMemberId)];
  }
  stored.routine = next;
  return reproject(stored, todayIsoDate());
}

export function fixtureDeleteRoutine(id: string): boolean {
  return STORE.delete(id);
}

/** A tiny catalog so Suggest does something believable offline. */
const CATALOG: ReadonlyArray<{ match: RegExp; estimate: EstimateResponse }> = [
  {
    match: /cabin\s*(air)?\s*filter/i,
    estimate: {
      intervalDays: 365,
      rangeDays: [180, 365],
      confidence: 0.8,
      source: "catalog",
      rationale: "Most manufacturers replace the cabin filter every 12 months or 15,000 mi.",
      usageDependent: true,
      followUpQuestion: null,
      catalogKey: "cabin_air_filter",
      category: "vehicle",
      anchor: "elapsed",
    },
  },
  {
    match: /haircut|hair\s*cut/i,
    estimate: {
      intervalDays: 42,
      rangeDays: [28, 56],
      confidence: 0.7,
      source: "catalog",
      rationale: "Short styles hold about 4–6 weeks; longer hair stretches to 8–12.",
      usageDependent: false,
      followUpQuestion: null,
      catalogKey: "haircut",
      category: "personal_care",
      anchor: "elapsed",
    },
  },
  {
    match: /smoke|co\b|carbon monoxide/i,
    estimate: {
      intervalDays: 182,
      rangeDays: [180, 365],
      confidence: 0.9,
      source: "catalog",
      rationale: "Test monthly, change batteries twice a year, replace the unit at 10 years.",
      usageDependent: false,
      followUpQuestion: null,
      catalogKey: "smoke_detector_batteries",
      category: "home",
      anchor: "elapsed",
    },
  },
];

export function fixtureEstimate(name: string, context?: string): EstimateResponse {
  const hit = CATALOG.find((c) => c.match.test(name));
  if (hit !== undefined) return hit.estimate;
  return {
    intervalDays: 90,
    rangeDays: [60, 120],
    confidence: 0.4,
    source: "estimated",
    rationale: `Sample estimate for “${name}”${context ? ` (${context})` : ""}: about every three months. Connect the API for a real one.`,
    usageDependent: true,
    followUpQuestion: "How often did you do this last year?",
    catalogKey: null,
    category: null,
    anchor: "elapsed",
  };
}

/**
 * The due events the fake server would have written to the agenda, one per
 * dated routine. Overdue rolls forward to today (contract ground rule 3).
 */
export function fixtureDueEvents(today: string): AllDayEventRow[] {
  const out: AllDayEventRow[] = [];
  for (const stored of STORE.values()) {
    const r = reproject(stored, today);
    if (r.dueOn === null || r.dueEventId === null) continue;
    const on = r.dueOn < today ? today : r.dueOn;
    out.push({
      kind: "event",
      eventId: r.dueEventId,
      title: r.name,
      tier: r.tier,
      tierSource: "auto",
      ownerMemberId: r.ownerMemberId,
      memberIds: r.memberIds,
      visibility: r.visibility,
      isFamily: r.memberIds.length > 1 && r.tier === "T1",
      status: "confirmed",
      routineId: r.routineId,
      allDay: true,
      startLocal: on,
      endLocal: on,
    });
  }
  return out;
}
