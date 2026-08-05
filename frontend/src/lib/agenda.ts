/** Pure selectors and geometry over an agenda payload. No React in here. */

import type {
  AgendaDay,
  AgendaResponse,
  AgendaRow,
  BusyRow,
  EventRow,
  Member,
  TimedEventRow,
} from "../types";
import { isBusyRow, isEventRow } from "../types";
import { addDays, daysBetween, minutesOfDay } from "./format";

export type MemberIndex = ReadonlyMap<string, Member>;

export function indexMembers(members: readonly Member[]): MemberIndex {
  return new Map(members.map((m) => [m.memberId, m]));
}

/**
 * Names for a set of member ids, in roster order where possible. An id with no
 * roster entry still renders *something* - a nameless colored row would encode
 * identity in color alone, which the PRD forbids outright.
 */
export function memberNames(index: MemberIndex, ids: readonly string[]): string[] {
  return ids.map((id) => index.get(id)?.displayName ?? id);
}

export function findDay(agenda: AgendaResponse, date: string): AgendaDay | undefined {
  return agenda.days.find((day) => day.date === date);
}

/** `count` days starting at `start`, filling gaps with empty days. */
export function daySpan(agenda: AgendaResponse, start: string, count: number): AgendaDay[] {
  const out: AgendaDay[] = [];
  for (let i = 0; i < count; i += 1) {
    const date = addDays(start, i);
    out.push(findDay(agenda, date) ?? { date, rows: [] });
  }
  return out;
}

export function busyRows(day: AgendaDay): BusyRow[] {
  return day.rows.filter(isBusyRow);
}

export function eventRows(day: AgendaDay): EventRow[] {
  return day.rows.filter(isEventRow);
}

/**
 * Contract ordering: busy bands first (roster order), then events by start,
 * all-day ahead of timed. The server already sorts; we re-apply it so a
 * hand-rolled fixture or a future source can't reorder the kitchen screen.
 */
export function orderedRows(day: AgendaDay, index: MemberIndex): AgendaRow[] {
  const order = [...index.keys()];
  const rank = (id: string): number => {
    const i = order.indexOf(id);
    return i === -1 ? order.length : i;
  };
  const bands = [...busyRows(day)].sort((a, b) => rank(a.memberId) - rank(b.memberId));
  const events = [...eventRows(day)].sort(compareEvents);
  return [...bands, ...events];
}

/**
 * All-day rows first, then by start. Safe to compare the raw strings: an
 * all-day `startLocal` is a bare date and a timed one is ISO, so both sort
 * lexicographically in chronological order without being parsed.
 */
export function compareEvents(a: EventRow, b: EventRow): number {
  if (a.allDay !== b.allDay) return a.allDay ? -1 : 1;
  return a.startLocal.localeCompare(b.startLocal);
}

export function dayIsEmpty(day: AgendaDay): boolean {
  return day.rows.length === 0;
}

/** The window the week-view bars are drawn against: 6am to 10pm. */
export const DAY_WINDOW_START_MIN = 6 * 60;
export const DAY_WINDOW_END_MIN = 22 * 60;
const WINDOW_SPAN = DAY_WINDOW_END_MIN - DAY_WINDOW_START_MIN;

export interface BarGeometry {
  /** Fraction 0..1 from the top of the window. */
  top: number;
  /** Fraction 0..1 of the window height. */
  height: number;
}

/** Clamped so a 5am flight or an 11pm meeting still shows an honest sliver. */
export function barGeometry(startLocal: string, endLocal: string): BarGeometry {
  const rawStart = minutesOfDay(startLocal);
  const rawEnd = Math.max(minutesOfDay(endLocal), rawStart + 15);
  const start = Math.min(Math.max(rawStart, DAY_WINDOW_START_MIN), DAY_WINDOW_END_MIN);
  const end = Math.min(Math.max(rawEnd, DAY_WINDOW_START_MIN), DAY_WINDOW_END_MIN);
  const top = (start - DAY_WINDOW_START_MIN) / WINDOW_SPAN;
  const height = Math.max((end - start) / WINDOW_SPAN, 0.04);
  return { top, height: Math.min(height, 1 - top) };
}

/**
 * Shift a whole payload by N days so the bundled fixture reads as *this* week.
 * Pure string surgery on the date portion - no Date arithmetic touches the
 * floating local strings.
 */
export function rebaseAgenda(agenda: AgendaResponse, anchorDate: string): AgendaResponse {
  const delta = daysBetween(agenda.range.start, anchorDate);
  if (delta === 0) return agenda;
  return {
    ...agenda,
    range: {
      ...agenda.range,
      start: addDays(agenda.range.start, delta),
      end: addDays(agenda.range.end, delta),
    },
    members: agenda.members,
    days: agenda.days.map((day) => ({
      date: addDays(day.date, delta),
      rows: day.rows.map((row) => shiftRow(row, delta)),
    })),
  };
}

export function shiftDateInString(value: string, days: number): string {
  const m = /^(\d{4}-\d{2}-\d{2})(.*)$/.exec(value);
  const head = m?.[1];
  if (head === undefined) return value;
  return addDays(head, days) + (m?.[2] ?? "");
}

export function shiftRow(row: AgendaRow, days: number): AgendaRow {
  if (isBusyRow(row)) {
    return {
      ...row,
      startLocal: shiftDateInString(row.startLocal, days),
      endLocal: shiftDateInString(row.endLocal, days),
    };
  }
  if (row.allDay) {
    // Bare dates, inclusive end - both shift by the same whole number of days.
    return {
      ...row,
      startLocal: shiftDateInString(row.startLocal, days),
      endLocal: shiftDateInString(row.endLocal, days),
    };
  }
  const next: TimedEventRow = { ...row, startLocal: shiftDateInString(row.startLocal, days) };
  if (next.endLocal !== undefined) next.endLocal = shiftDateInString(next.endLocal, days);
  if (next.startUtc !== undefined) next.startUtc = shiftDateInString(next.startUtc, days);
  return next;
}
