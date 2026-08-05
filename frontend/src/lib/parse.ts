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
  BusyRow,
  EventRow,
  Member,
  MemberRole,
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
  };

  const optional: Pick<EventRow, "location" | "occurrenceId" | "startUtc"> = {};
  const location = asString(value["location"]);
  if (location !== undefined) optional.location = location;
  const occurrenceId = asString(value["occurrenceId"]);
  if (occurrenceId !== undefined) optional.occurrenceId = occurrenceId;
  const startUtc = asString(value["startUtc"]);
  if (startUtc !== undefined) optional.startUtc = startUtc;

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
