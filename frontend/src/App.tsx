import { useEffect, useState } from "react";

const TIME_FMT = new Intl.DateTimeFormat("en-US", {
  hour: "numeric",
  minute: "2-digit",
});

const DATE_FMT = new Intl.DateTimeFormat("en-US", {
  weekday: "long",
  month: "long",
  day: "numeric",
});

export function App() {
  const [now, setNow] = useState(() => new Date());

  useEffect(() => {
    // Tick on the minute boundary rather than every second - the display shows
    // minutes, and a 1s interval keeps the panel awake for nothing.
    let timer: number;
    const schedule = () => {
      const msToNextMinute = 60_000 - (Date.now() % 60_000);
      timer = window.setTimeout(() => {
        setNow(new Date());
        schedule();
      }, msToNextMinute);
    };
    schedule();
    return () => window.clearTimeout(timer);
  }, []);

  return (
    <main className="shell">
      <div>
        <div className="clock">{TIME_FMT.format(now)}</div>
        <div className="date">{DATE_FMT.format(now)}</div>
      </div>
      <div className="rule" />
      <div>
        <div className="brand">Airhead Calendar</div>
        <p className="tagline">
          Gets everything out of my airy head and into the calendar.
        </p>
      </div>
      <div className="milestone">M0 — infrastructure online</div>
    </main>
  );
}
