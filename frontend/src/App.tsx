import { useMemo, useState } from "react";

import { usingFixture } from "./api";
import { DayView } from "./components/DayView";
import { DisplayHeader } from "./components/DisplayHeader";
import { NavBar, type ViewMode } from "./components/NavBar";
import { StatusNote } from "./components/StatusNote";
import { WeekView } from "./components/WeekView";
import { useAgenda } from "./hooks/useAgenda";
import { useNow } from "./hooks/useNow";
import { daySpan, findDay, indexMembers } from "./lib/agenda";
import { addDays, daysBetween, formatMonthDay, todayIsoDate } from "./lib/format";

const WEEK_DAYS = 7;

export function App() {
  const now = useNow();
  const today = todayIsoDate(now);

  const [view, setView] = useState<ViewMode>("day");
  const [selected, setSelected] = useState(today);

  // One request covers every view: the day, tomorrow and the week are all
  // slices of the same window, so tapping between them is instant and the
  // kiosk makes one call every five minutes instead of one per tap.
  const windowStart = useMemo(() => {
    const offset = daysBetween(today, selected);
    return offset >= 0 && offset < WEEK_DAYS ? today : selected;
  }, [today, selected]);

  const windowEnd = addDays(windowStart, WEEK_DAYS - 1);
  const { data, status, error, fetchedAt, stale, reload } = useAgenda(windowStart, windowEnd);

  const members = useMemo(() => indexMembers(data?.members ?? []), [data]);
  const weekDays = useMemo(
    () => (data === null ? [] : daySpan(data, windowStart, WEEK_DAYS)),
    [data, windowStart],
  );

  const headerDate = view === "week" ? windowStart : selected;
  const isToday = selected === today;
  const isTomorrow = selected === addDays(today, 1);

  const context =
    view === "week"
      ? `Week of ${formatMonthDay(windowStart)} – ${formatMonthDay(windowEnd)}`
      : isToday
        ? "Today"
        : isTomorrow
          ? "Tomorrow"
          : undefined;

  const step = (delta: number) => {
    if (view === "week") {
      setSelected((d) => addDays(d, delta * WEEK_DAYS));
      return;
    }
    setSelected((d) => addDays(d, delta));
  };

  return (
    <main className="shell">
      <DisplayHeader date={headerDate} now={now} {...(context ? { context } : {})} />
      {/* A stable grid child even when there is nothing to say - otherwise the
          rows shift up and the nav bar claims the content row's 1fr. */}
      <div className="status-slot">
        <StatusNote fixture={usingFixture} stale={stale} fetchedAt={fetchedAt} now={now} />
      </div>

      <div className="content">
        {status === "loading" ? (
          <p className="content__note">Loading the day…</p>
        ) : status === "error" || data === null ? (
          <div className="content__error">
            <p className="content__note">Can’t reach the calendar.</p>
            {error !== null ? <p className="content__detail">{error}</p> : null}
            <button type="button" className="nav__btn" onClick={reload}>
              Try again
            </button>
          </div>
        ) : view === "week" ? (
          <WeekView days={weekDays} members={members} today={today} />
        ) : (
          <DayView day={findDay(data, selected) ?? { date: selected, rows: [] }} members={members} />
        )}
      </div>

      <NavBar
        view={view}
        isToday={isToday}
        isTomorrow={isTomorrow}
        onPrev={() => step(-1)}
        onNext={() => step(1)}
        onToday={() => {
          setView("day");
          setSelected(today);
        }}
        onTomorrow={() => {
          setView("day");
          setSelected(addDays(today, 1));
        }}
        onWeek={() => setView("week")}
      />
    </main>
  );
}
