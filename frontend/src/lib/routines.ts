/** Pure helpers over routine payloads. No React, no fetch. */

import type { Routine, RoutineStatus } from "../types";
import { daysBetween } from "./format";

/**
 * Contract ordering for `GET /api/routines`: overdue (most overdue first) →
 * due_soon → ok (soonest first) → unscheduled → paused. The server already
 * sorts; re-applied here so an optimistic local update or a fixture can't put
 * an overdue row anywhere but the top.
 */
const STATUS_RANK: Record<RoutineStatus, number> = {
  overdue: 0,
  due_soon: 1,
  ok: 2,
  unscheduled: 3,
  paused: 4,
};

export function compareRoutines(a: Routine, b: Routine): number {
  const rank = STATUS_RANK[a.status] - STATUS_RANK[b.status];
  if (rank !== 0) return rank;
  // Within a dated group, soonest (or most overdue) first. Undated rows keep
  // their wire order via a stable sort.
  const da = a.daysUntilDue ?? Number.MAX_SAFE_INTEGER;
  const db = b.daysUntilDue ?? Number.MAX_SAFE_INTEGER;
  if (da !== db) return da - db;
  return a.name.localeCompare(b.name);
}

export function orderRoutines(routines: readonly Routine[]): Routine[] {
  return [...routines].sort(compareRoutines);
}

/** "today" / "yesterday" / "12 days ago" - a date the server sent, restated. */
export function lastDoneLabel(lastDoneOn: string | null, today: string): string {
  if (lastDoneOn === null) return "never done";
  const days = daysBetween(lastDoneOn, today);
  if (days <= 0) return "done today";
  if (days === 1) return "done yesterday";
  return `done ${days} days ago`;
}

/** "every 35 days" / "every year" - a restatement of what the server holds. */
export function intervalLabel(routine: Routine): string | null {
  const n = routine.intervalDays;
  if (n === null) return null;
  if (routine.anchor === "calendar") return "same day every year";
  if (n === 1) return "every day";
  if (n === 7) return "every week";
  if (n === 365) return "every year";
  return `every ${n} days`;
}

/** "3 days overdue" / "due today" / "due in 5 days" from the server's own count. */
export function dueLabel(routine: Routine): string | null {
  const d = routine.daysUntilDue;
  if (d === null) return null;
  if (d < -1) return `${-d} days overdue`;
  if (d === -1) return "1 day overdue";
  if (d === 0) return "due today";
  if (d === 1) return "due tomorrow";
  return `due in ${d} days`;
}

/** Text for the status chip. Text, never colour alone (PRD §11). */
export function statusChip(routine: Routine): { text: string; kind: string } | null {
  switch (routine.status) {
    case "overdue":
      return { text: "Overdue", kind: "overdue" };
    case "due_soon":
      return { text: "Due soon", kind: "due-soon" };
    case "paused":
      return { text: "Paused", kind: "paused" };
    case "unscheduled":
      return { text: "Unscheduled", kind: "unscheduled" };
    case "ok":
      return null;
  }
}
