import { formatMonthDayLong, formatWeekdayLong } from "../lib/format";

const CLOCK_FMT = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });

interface DisplayHeaderProps {
  /** The day currently on screen (YYYY-MM-DD). */
  date: string;
  /** Wall clock, ticked on the minute boundary by `useNow`. */
  now: Date;
  /** e.g. "Tomorrow" or "Week of…" - context when the view isn't today. */
  context?: string;
}

export function DisplayHeader({ date, now, context }: DisplayHeaderProps) {
  return (
    <header className="header">
      <div className="header__date">
        <span className="header__weekday">{formatWeekdayLong(date)}</span>
        <span className="header__monthday">{formatMonthDayLong(date)}</span>
        {context !== undefined ? <span className="header__context">{context}</span> : null}
      </div>
      {/* The live clock is the household's, not the payload's: the kiosk hangs
          in the kitchen, so its own wall clock is the right one to show. */}
      <time className="header__clock" dateTime={now.toISOString()}>
        {CLOCK_FMT.format(now)}
      </time>
    </header>
  );
}
