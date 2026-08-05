/**
 * Hand-written mirror of `docs/M1-CONTRACT.md`.
 *
 * Two rules from the contract shape everything here:
 *  1. Unknown fields are additive - the display ignores what it doesn't know,
 *     so these types are a *subset* of the wire, never an exhaustive one.
 *  2. `startLocal` / `endLocal` are floating household-local wall-clock strings
 *     ("2026-08-04T16:00:00"). They are NOT instants. Never hand one to `new
 *     Date()` for arithmetic - see `src/lib/format.ts`.
 */

export type Tier = "T1" | "T2" | "T3";
export type TierSource = "auto" | "human";
export type MemberRole = "adult" | "minor";
export type Visibility = "all" | "adults";

export interface Member {
  memberId: string;
  displayName: string;
  role: MemberRole;
  /** Hex. Identity is never carried by color alone - the name label rides along. */
  color: string;
}

export interface AgendaRangeInfo {
  /** YYYY-MM-DD, inclusive, household-local. */
  start: string;
  /** YYYY-MM-DD, inclusive. */
  end: string;
  /** Household IANA zone. Informational only; the browser does no math with it. */
  tz: string;
}

interface EventRowBase {
  kind: "event";
  eventId: string;
  title: string;
  tier: Tier;
  tierSource: TierSource;
  ownerMemberId: string;
  /**
   * Deduped set of owner + involves, already ordered to match the roster order
   * in `members`. Use it directly - re-sorting would only desynchronise the
   * label order from the color order.
   */
  memberIds: string[];
  location?: string;
  visibility: Visibility;
  /** memberIds.length > 1 && tier === "T1" - drives the FAMILY chip. */
  isFamily: boolean;
  occurrenceId?: string;
  /** True instant. Present for ordering/debug; the display never positions with it. */
  startUtc?: string;
}

/** A timed row: floating household-local datetimes, "2026-08-04T16:00:00". */
export interface TimedEventRow extends EventRowBase {
  allDay: false;
  startLocal: string;
  endLocal?: string;
}

/**
 * An all-day row: `startLocal` / `endLocal` are **bare dates**, and `endLocal`
 * is **inclusive** - the last day the event covers. A one-day all-day event
 * therefore has `startLocal === endLocal`.
 *
 * The API deliberately does not send an exclusive next-midnight end, because
 * that is the classic all-day off-by-one. The display must not "correct" it
 * with arithmetic of its own.
 */
export interface AllDayEventRow extends EventRowBase {
  allDay: true;
  /** YYYY-MM-DD */
  startLocal: string;
  /** YYYY-MM-DD, inclusive. */
  endLocal: string;
}

/**
 * Discriminated on `allDay` so the two date formats can never be confused:
 * TypeScript forces the branch before either string is read.
 */
export type EventRow = TimedEventRow | AllDayEventRow;

/**
 * The T3 collapse. Structural, not cosmetic: a day with work meetings always
 * emits one of these carrying a truthful `count`. It is never omitted and the
 * display must never hide it (PRD R6 - trust collapse is the product-killing
 * failure). Absence of a band means absence of work, which is itself
 * information the household reads.
 */
export interface BusyRow {
  kind: "busy";
  memberId: string;
  startLocal: string;
  endLocal: string;
  count: number;
  /** For the tap-to-expand inline list. */
  eventIds: string[];
}

export type AgendaRow = EventRow | BusyRow;

export interface AgendaDay {
  /** YYYY-MM-DD */
  date: string;
  rows: AgendaRow[];
}

export interface AgendaResponse {
  range: AgendaRangeInfo;
  members: Member[];
  days: AgendaDay[];
}

export function isBusyRow(row: AgendaRow): row is BusyRow {
  return row.kind === "busy";
}

export function isEventRow(row: AgendaRow): row is EventRow {
  return row.kind === "event";
}
