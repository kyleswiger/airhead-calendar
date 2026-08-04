import { useEffect, useState } from "react";

/**
 * The wall clock, re-rendered once per minute.
 *
 * Tick on the minute boundary rather than every second - the display shows
 * minutes, and a 1s interval keeps the panel awake for nothing. (Carried over
 * from the M0 placeholder; the reason still holds and matters more now that
 * the panel is meant to dim and sleep.)
 */
export function useNow(): Date {
  const [now, setNow] = useState(() => new Date());

  useEffect(() => {
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

  return now;
}
