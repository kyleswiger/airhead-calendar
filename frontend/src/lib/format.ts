/**
 * Pure formatting helpers. No React, no fetch - importable by a test runner.
 *
 * THE TIMEZONE RULE: the server sends wall-clock strings already converted to
 * household time. This module does no timezone math. Times are read out of the
 * string with a regex; dates are formatted through an Intl formatter pinned to
 * UTC over a UTC-constructed instant, so the browser's own zone and its DST
 * rules can never shift a row. A DST bug on a kitchen wall goes unnoticed for
 * six months, so the browser is simply not allowed to have an opinion.
 */

const TIME_FMT = new Intl.DateTimeFormat(undefined, {
  hour: "numeric",
  minute: "2-digit",
  timeZone: "UTC",
});

const WEEKDAY_LONG_FMT = new Intl.DateTimeFormat(undefined, {
  weekday: "long",
  timeZone: "UTC",
});

const WEEKDAY_SHORT_FMT = new Intl.DateTimeFormat(undefined, {
  weekday: "short",
  timeZone: "UTC",
});

const MONTH_DAY_FMT = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  timeZone: "UTC",
});

const MONTH_DAY_LONG_FMT = new Intl.DateTimeFormat(undefined, {
  month: "long",
  day: "numeric",
  timeZone: "UTC",
});

const DAY_MS = 86_400_000;

export interface WallClock {
  hour: number;
  minute: number;
}

export interface DateParts {
  year: number;
  month: number;
  day: number;
}

/** "2026-08-04T16:00:00" -> { hour: 16, minute: 0 }. Never constructs a Date. */
export function wallClock(local: string): WallClock | null {
  const m = /T(\d{2}):(\d{2})/.exec(local);
  if (!m) return null;
  const hour = Number(m[1]);
  const minute = Number(m[2]);
  if (!Number.isFinite(hour) || !Number.isFinite(minute)) return null;
  return { hour, minute };
}

/** "2026-08-04..." -> { year, month, day }. Accepts a date or a datetime. */
export function dateParts(iso: string): DateParts | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  if (!m) return null;
  const year = Number(m[1]);
  const month = Number(m[2]);
  const day = Number(m[3]);
  if (!Number.isFinite(year) || !Number.isFinite(month) || !Number.isFinite(day)) {
    return null;
  }
  return { year, month, day };
}

/** YYYY-MM-DD -> a UTC-anchored timestamp, used only as Intl formatter input. */
function utcStamp(isoDate: string): number | null {
  const p = dateParts(isoDate);
  if (!p) return null;
  return Date.UTC(p.year, p.month - 1, p.day);
}

/** "2026-08-04T16:00:00" -> "4:00 PM" (locale-shaped). */
export function formatTime(local: string): string {
  const t = wallClock(local);
  if (!t) return "";
  return TIME_FMT.format(Date.UTC(2000, 0, 1, t.hour, t.minute));
}

/** "9:00 AM – 3:00 PM" */
export function formatTimeRange(startLocal: string, endLocal: string): string {
  const start = formatTime(startLocal);
  const end = formatTime(endLocal);
  if (!start) return end;
  if (!end) return start;
  return `${start} – ${end}`;
}

export function formatWeekdayLong(isoDate: string): string {
  const stamp = utcStamp(isoDate);
  return stamp === null ? isoDate : WEEKDAY_LONG_FMT.format(stamp);
}

export function formatWeekdayShort(isoDate: string): string {
  const stamp = utcStamp(isoDate);
  return stamp === null ? isoDate : WEEKDAY_SHORT_FMT.format(stamp);
}

export function formatMonthDay(isoDate: string): string {
  const stamp = utcStamp(isoDate);
  return stamp === null ? isoDate : MONTH_DAY_FMT.format(stamp);
}

export function formatMonthDayLong(isoDate: string): string {
  const stamp = utcStamp(isoDate);
  return stamp === null ? isoDate : MONTH_DAY_LONG_FMT.format(stamp);
}

/** Day-of-month as a bare numeral, for the week-view column heads. */
export function dayOfMonth(isoDate: string): string {
  const p = dateParts(isoDate);
  return p === null ? "" : String(p.day);
}

function pad2(n: number): string {
  return n < 10 ? `0${n}` : String(n);
}

export function toIsoDate(parts: DateParts): string {
  return `${parts.year}-${pad2(parts.month)}-${pad2(parts.day)}`;
}

/** Calendar arithmetic done entirely in UTC so no offset transition can bite. */
export function addDays(isoDate: string, days: number): string {
  const stamp = utcStamp(isoDate);
  if (stamp === null) return isoDate;
  const shifted = new Date(stamp + days * DAY_MS);
  return toIsoDate({
    year: shifted.getUTCFullYear(),
    month: shifted.getUTCMonth() + 1,
    day: shifted.getUTCDate(),
  });
}

/** Whole days from `a` to `b`. Both are plain YYYY-MM-DD. */
export function daysBetween(a: string, b: string): number {
  const from = utcStamp(a);
  const to = utcStamp(b);
  if (from === null || to === null) return 0;
  return Math.round((to - from) / DAY_MS);
}

/**
 * Today, per the wall clock of the machine the display runs on. The kiosk is
 * physically in the household, so its local date is the household date - the
 * one and only place the browser's clock is trusted, and only to the day.
 */
export function todayIsoDate(now: Date = new Date()): string {
  return toIsoDate({
    year: now.getFullYear(),
    month: now.getMonth() + 1,
    day: now.getDate(),
  });
}

/** Minutes since midnight, for bar geometry. */
export function minutesOfDay(local: string): number {
  const t = wallClock(local);
  return t === null ? 0 : t.hour * 60 + t.minute;
}

/** "5 meetings" / "1 meeting" - the count is the promise nothing is hidden. */
export function pluralize(count: number, singular: string, plural?: string): string {
  return `${count} ${count === 1 ? singular : (plural ?? `${singular}s`)}`;
}

/** "just now" / "12 min ago" / "2 hr ago" - staleness, not precision. */
export function formatAge(ms: number): string {
  const minutes = Math.floor(ms / 60_000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} hr ago`;
  return `${Math.floor(hours / 24)} d ago`;
}
