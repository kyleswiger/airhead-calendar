/**
 * Defensive decoding of the wire into `src/types.ts`.
 *
 * Two jobs, both from the contract:
 *  - ignore unknown fields (additive responses must not break a wall display);
 *  - never trust a field's type. A malformed row is dropped, not thrown on -
 *    losing one row beats blanking the kitchen screen.
 *
 * No `any`, no non-null assertions: every read narrows or falls back.
 */

import type {
  AgendaDay,
  AgendaResponse,
  AgendaRow,
  AgentAction,
  AgentTurnResponse,
  AgentUsage,
  BusyRow,
  Completion,
  EstimateResponse,
  EventRow,
  EventStatus,
  IntervalSource,
  Member,
  MemberRole,
  PendingConfirmation,
  Routine,
  RoutineAnchor,
  RoutineStatus,
  Tier,
  TierSource,
  TimedEventRow,
  Visibility,
} from "../types";

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function asString(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

function asBoolean(value: unknown, fallback: boolean): boolean {
  return typeof value === "boolean" ? value : fallback;
}

function asCount(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? Math.floor(value)
    : undefined;
}

function asStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((entry): entry is string => typeof entry === "string");
}

function asOneOf<T extends string>(value: unknown, allowed: readonly T[], fallback: T): T {
  const s = asString(value);
  return s !== undefined && (allowed as readonly string[]).includes(s) ? (s as T) : fallback;
}

const TIERS: readonly Tier[] = ["T1", "T2", "T3"];
const TIER_SOURCES: readonly TierSource[] = ["auto", "human"];
const ROLES: readonly MemberRole[] = ["adult", "minor"];
const VISIBILITIES: readonly Visibility[] = ["all", "adults"];
const STATUSES: readonly EventStatus[] = ["proposed", "confirmed"];

export function parseMember(value: unknown): Member | null {
  if (!isRecord(value)) return null;
  const memberId = asString(value["memberId"]);
  if (memberId === undefined) return null;
  return {
    memberId,
    displayName: asString(value["displayName"]) ?? memberId,
    role: asOneOf(value["role"], ROLES, "adult"),
    color: asString(value["color"]) ?? "#8b93a7",
  };
}

/** Bare YYYY-MM-DD. Slices rather than converts - no arithmetic, no timezone. */
function bareDate(value: string): string | null {
  return /^\d{4}-\d{2}-\d{2}/.test(value) ? value.slice(0, 10) : null;
}

function parseEventRow(value: Record<string, unknown>): EventRow | null {
  const eventId = asString(value["eventId"]);
  if (eventId === undefined) return null;

  const startLocal = asString(value["startLocal"]);
  if (startLocal === undefined) return null; // Unpositionable: drop, don't guess.

  const memberIds = asStringArray(value["memberIds"]);
  const ownerMemberId = asString(value["ownerMemberId"]) ?? memberIds[0] ?? "";
  const tier = asOneOf(value["tier"], TIERS, "T1");
  const endLocal = asString(value["endLocal"]);

  const common = {
    kind: "event" as const,
    eventId,
    title: asString(value["title"]) ?? "Untitled",
    tier,
    tierSource: asOneOf(value["tierSource"], TIER_SOURCES, "auto"),
    ownerMemberId,
    // Already roster-ordered on the wire; preserved as-is.
    memberIds: memberIds.length > 0 ? memberIds : ownerMemberId ? [ownerMemberId] : [],
    visibility: asOneOf(value["visibility"], VISIBILITIES, "all"),
    // Recompute rather than trust: the chip must agree with what is on screen.
    isFamily: asBoolean(value["isFamily"], false) || (memberIds.length > 1 && tier === "T1"),
    // Older responses omit it; absent means confirmed, exactly as the API defaults.
    status: asOneOf(value["status"], STATUSES, "confirmed"),
  };

  const optional: Pick<EventRow, "location" | "occurrenceId" | "startUtc" | "routineId"> = {};
  const location = asString(value["location"]);
  if (location !== undefined) optional.location = location;
  const occurrenceId = asString(value["occurrenceId"]);
  if (occurrenceId !== undefined) optional.occurrenceId = occurrenceId;
  const startUtc = asString(value["startUtc"]);
  if (startUtc !== undefined) optional.startUtc = startUtc;
  const routineId = asString(value["routineId"]);
  if (routineId !== undefined) optional.routineId = routineId;

  if (asBoolean(value["allDay"], false)) {
    // Bare dates, and `endLocal` is the INCLUSIVE last covered day. A one-day
    // event has start === end; a missing end means exactly one day.
    const start = bareDate(startLocal);
    if (start === null) return null;
    const end = endLocal === undefined ? null : bareDate(endLocal);
    return { ...common, ...optional, allDay: true, startLocal: start, endLocal: end ?? start };
  }

  const timed: TimedEventRow = { ...common, ...optional, allDay: false, startLocal };
  if (endLocal !== undefined) timed.endLocal = endLocal;
  return timed;
}

function parseBusyRow(value: Record<string, unknown>): BusyRow | null {
  const memberId = asString(value["memberId"]);
  const startLocal = asString(value["startLocal"]);
  const endLocal = asString(value["endLocal"]);
  if (memberId === undefined || startLocal === undefined || endLocal === undefined) {
    return null;
  }
  const eventIds = asStringArray(value["eventIds"]);
  // The count is the promise that nothing is invisible. If the server omitted
  // it, fall back to the id list rather than rendering a bandless day.
  const count = asCount(value["count"]) ?? eventIds.length;
  return { kind: "busy", memberId, startLocal, endLocal, count, eventIds };
}

export function parseRow(value: unknown): AgendaRow | null {
  if (!isRecord(value)) return null;
  const kind = asString(value["kind"]);
  if (kind === "busy") return parseBusyRow(value);
  if (kind === "event") return parseEventRow(value);
  return null; // A future row kind we don't understand: ignore, don't crash.
}

export function parseDay(value: unknown): AgendaDay | null {
  if (!isRecord(value)) return null;
  const date = asString(value["date"]);
  if (date === undefined) return null;
  const raw = value["rows"];
  const rows: AgendaRow[] = Array.isArray(raw)
    ? raw.map(parseRow).filter((row): row is AgendaRow => row !== null)
    : [];
  return { date, rows };
}

/** Throws only when the envelope itself is unusable. */
export function parseAgenda(value: unknown): AgendaResponse {
  if (!isRecord(value)) throw new Error("Agenda response was not an object");

  const rangeRaw = value["range"];
  const range = isRecord(rangeRaw) ? rangeRaw : {};
  const start = asString(range["start"]);
  const end = asString(range["end"]);
  if (start === undefined || end === undefined) {
    throw new Error("Agenda response is missing range.start / range.end");
  }

  const membersRaw = value["members"];
  const members = Array.isArray(membersRaw)
    ? membersRaw.map(parseMember).filter((m): m is Member => m !== null)
    : [];

  const daysRaw = value["days"];
  const days = Array.isArray(daysRaw)
    ? daysRaw.map(parseDay).filter((d): d is AgendaDay => d !== null)
    : [];

  return {
    range: { start, end, tz: asString(range["tz"]) ?? "" },
    members,
    days,
  };
}

/* ------------------------------------------------------------------ M2 -- */

function parseAction(value: unknown): AgentAction | null {
  if (!isRecord(value)) return null;
  const tool = asString(value["tool"]);
  if (tool === undefined) return null;
  // An action with no status is not evidence that anything was applied, so it
  // is reported as-is rather than assumed "ok".
  const action: AgentAction = { tool, status: asString(value["status"]) ?? "unknown" };
  const eventId = asString(value["eventId"]);
  if (eventId !== undefined) action.eventId = eventId;
  return action;
}

/**
 * A gate is only a gate if we can answer it. Without a `callId` there is no
 * round-trip to make, and without a `summary` there is nothing truthful to put
 * in front of a human - in both cases we drop it rather than render a confirm
 * button that would either fail or ask about nothing.
 */
function parsePending(value: unknown): PendingConfirmation | null {
  if (!isRecord(value)) return null;
  const callId = asString(value["callId"]);
  const summary = asString(value["summary"]);
  if (callId === undefined || summary === undefined) return null;
  const pending: PendingConfirmation = {
    callId,
    tool: asString(value["tool"]) ?? "",
    summary,
  };
  const eventId = asString(value["eventId"]);
  if (eventId !== undefined) pending.eventId = eventId;
  return pending;
}

function parseUsage(value: unknown): AgentUsage | null {
  if (!isRecord(value)) return null;
  return {
    inputTokens: asCount(value["inputTokens"]) ?? 0,
    outputTokens: asCount(value["outputTokens"]) ?? 0,
    cacheReadInputTokens: asCount(value["cacheReadInputTokens"]) ?? 0,
  };
}

/** Throws only when the envelope itself is unusable. */
export function parseAgentTurn(value: unknown): AgentTurnResponse {
  if (!isRecord(value)) throw new Error("Agent response was not an object");

  const conversationId = asString(value["conversationId"]);
  if (conversationId === undefined) {
    // Without it the next turn would silently start a new conversation, and
    // the confirmation round-trip would answer a gate nobody is holding.
    throw new Error("Agent response is missing conversationId");
  }

  const rawActions = value["actions"];
  const actions = Array.isArray(rawActions)
    ? rawActions.map(parseAction).filter((a): a is AgentAction => a !== null)
    : [];

  const out: AgentTurnResponse = {
    conversationId,
    turnId: asString(value["turnId"]) ?? "",
    reply: asString(value["reply"]) ?? "",
    actions,
  };
  const pending = parsePending(value["pendingConfirmation"]);
  if (pending !== null) out.pendingConfirmation = pending;
  const usage = parseUsage(value["usage"]);
  if (usage !== null) out.usage = usage;
  return out;
}

/* ------------------------------------------------------------ routines -- */

const ROUTINE_STATUSES: readonly RoutineStatus[] = [
  "paused",
  "unscheduled",
  "overdue",
  "due_soon",
  "ok",
];
const INTERVAL_SOURCES: readonly IntervalSource[] = ["human", "observed", "catalog", "estimated"];
const ANCHORS: readonly RoutineAnchor[] = ["elapsed", "calendar"];

/** A finite integer, positive or negative; null for anything else. */
function asInt(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? Math.trunc(value) : null;
}

function asUnit(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1
    ? value
    : null;
}

function asBareDate(value: unknown): string | null {
  const s = asString(value);
  return s === undefined ? null : bareDate(s);
}

/**
 * A routine is only usable with an id and a name. Everything else falls back:
 * a routine the server forgot to grade lands as `unscheduled`, never as due -
 * an invented due date is worse than a missing one.
 */
export function parseRoutine(value: unknown): Routine | null {
  if (!isRecord(value)) return null;
  const routineId = asString(value["routineId"]);
  const name = asString(value["name"]);
  if (routineId === undefined || name === undefined) return null;

  const memberIds = asStringArray(value["memberIds"]);
  const ownerMemberId = asString(value["ownerMemberId"]) ?? memberIds[0] ?? "";
  const intervalDays = asInt(value["intervalDays"]);
  const source = asString(value["intervalSource"]);
  const paused = asBoolean(value["paused"], false);
  const dueOn = asBareDate(value["dueOn"]);

  return {
    routineId,
    name,
    category: asString(value["category"]) ?? "other",
    ownerMemberId,
    memberIds: memberIds.length > 0 ? memberIds : ownerMemberId ? [ownerMemberId] : [],
    tier: asOneOf(value["tier"], TIERS, "T2"),
    visibility: asOneOf(value["visibility"], VISIBILITIES, "all"),
    intervalDays: intervalDays !== null && intervalDays > 0 ? intervalDays : null,
    // The source is meaningless without an interval (contract, domain notes).
    intervalSource:
      intervalDays === null
        ? null
        : source !== undefined && (INTERVAL_SOURCES as readonly string[]).includes(source)
          ? (source as IntervalSource)
          : null,
    intervalNote: asString(value["intervalNote"]) ?? null,
    intervalConfidence: asUnit(value["intervalConfidence"]),
    anchor: asOneOf(value["anchor"], ANCHORS, "elapsed"),
    catalogKey: asString(value["catalogKey"]) ?? null,
    lastDoneOn: asBareDate(value["lastDoneOn"]),
    dueOn,
    dueEventId: asString(value["dueEventId"]) ?? null,
    status: asOneOf(value["status"], ROUTINE_STATUSES, paused ? "paused" : "unscheduled"),
    daysUntilDue: dueOn === null ? null : asInt(value["daysUntilDue"]),
    completionCount: asCount(value["completionCount"]) ?? 0,
    paused,
  };
}

/** `GET /api/routines`. Throws only when the envelope itself is unusable. */
export function parseRoutines(value: unknown): Routine[] {
  if (!isRecord(value)) throw new Error("Routines response was not an object");
  const raw = value["routines"];
  if (!Array.isArray(raw)) throw new Error("Routines response is missing routines[]");
  return raw.map(parseRoutine).filter((r): r is Routine => r !== null);
}

/** A single routine body, as returned by POST / PATCH / complete. */
export function parseRoutineEnvelope(value: unknown): Routine {
  const routine = parseRoutine(value);
  if (routine === null) throw new Error("Routine response was missing routineId / name");
  return routine;
}

export function parseCompletion(value: unknown): Completion | null {
  if (!isRecord(value)) return null;
  const completionId = asString(value["completionId"]);
  const doneOn = asBareDate(value["doneOn"]);
  if (completionId === undefined || doneOn === null) return null;
  const out: Completion = {
    completionId,
    doneOn,
    byMemberId: asString(value["byMemberId"]) ?? "",
  };
  const note = asString(value["note"]);
  if (note !== undefined) out.note = note;
  return out;
}

/** `GET /api/routines/{id}/completions`. */
export function parseCompletions(value: unknown): Completion[] {
  if (!isRecord(value)) throw new Error("Completions response was not an object");
  const raw = value["completions"];
  if (!Array.isArray(raw)) return [];
  return raw.map(parseCompletion).filter((c): c is Completion => c !== null);
}

/**
 * `POST /api/routines/estimate`. A reply with no rationale still counts - the
 * form shows the number and says nothing about why - but a reply that is not
 * an object is an error the form has to surface.
 */
export function parseEstimate(value: unknown): EstimateResponse {
  if (!isRecord(value)) throw new Error("Estimate response was not an object");
  const intervalDays = asInt(value["intervalDays"]);
  const rangeRaw = value["rangeDays"];
  let rangeDays: [number, number] | null = null;
  if (Array.isArray(rangeRaw) && rangeRaw.length === 2) {
    const lo = asInt(rangeRaw[0]);
    const hi = asInt(rangeRaw[1]);
    if (lo !== null && hi !== null) rangeDays = [lo, hi];
  }
  return {
    intervalDays: intervalDays !== null && intervalDays > 0 ? intervalDays : null,
    rangeDays,
    confidence: asUnit(value["confidence"]),
    source: asOneOf(value["source"], ["catalog", "estimated"] as const, "estimated"),
    rationale: asString(value["rationale"]) ?? "",
    usageDependent: asBoolean(value["usageDependent"], false),
    followUpQuestion: asString(value["followUpQuestion"]) ?? null,
    catalogKey: asString(value["catalogKey"]) ?? null,
    category: asString(value["category"]) ?? null,
    anchor: asOneOf(value["anchor"], ANCHORS, "elapsed"),
  };
}

export interface ApiErrorShape {
  code: string;
  message: string;
}

export function parseApiError(value: unknown): ApiErrorShape | null {
  if (!isRecord(value)) return null;
  const err = value["error"];
  if (!isRecord(err)) return null;
  const code = asString(err["code"]);
  const message = asString(err["message"]);
  if (code === undefined || message === undefined) return null;
  return { code, message };
}
