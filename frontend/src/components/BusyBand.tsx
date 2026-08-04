import { useCallback, useEffect, useRef, useState } from "react";

import { fetchEvents } from "../api";
import { barGeometry } from "../lib/agenda";
import { safeColor, withAlpha } from "../lib/color";
import { formatTime, formatTimeRange, pluralize } from "../lib/format";
import type { BusyRow, EventRow, Member } from "../types";
import { MemberTag } from "./MemberTag";

/** PRD §11: the expansion collapses again on a 30s idle timer. */
const IDLE_MS = 30_000;

interface BusyBandProps {
  row: BusyRow;
  member: Member | undefined;
}

type DetailState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ready"; events: EventRow[] }
  | { kind: "error" };

/**
 * The T3 collapse: one band per person per day, carrying a visible count.
 *
 * The count is the whole point (PRD R6). A band that said only "busy" would be
 * hiding something, and hiding something that mattered is the product-killing
 * failure. So the count renders from the row itself and stays on screen in
 * every state - expanded, collapsed, and when the detail fetch fails.
 */
export function BusyBand({ row, member }: BusyBandProps) {
  const [expanded, setExpanded] = useState(false);
  const [detail, setDetail] = useState<DetailState>({ kind: "idle" });
  const idleTimer = useRef<number | undefined>(undefined);

  const color = safeColor(member?.color);
  const geometry = barGeometry(row.startLocal, row.endLocal);
  const countLabel = pluralize(row.count, "meeting");
  const panelId = `busy-${row.memberId}-${row.startLocal}`;

  const resetIdle = useCallback(() => {
    window.clearTimeout(idleTimer.current);
    idleTimer.current = window.setTimeout(() => setExpanded(false), IDLE_MS);
  }, []);

  useEffect(() => {
    if (!expanded) {
      window.clearTimeout(idleTimer.current);
      return;
    }
    resetIdle();
    return () => window.clearTimeout(idleTimer.current);
  }, [expanded, resetIdle]);

  useEffect(() => {
    if (!expanded || detail.kind !== "idle") return;
    const controller = new AbortController();
    setDetail({ kind: "loading" });
    fetchEvents(row.eventIds, controller.signal)
      .then((events) => {
        if (!controller.signal.aborted) setDetail({ kind: "ready", events });
      })
      .catch(() => {
        if (!controller.signal.aborted) setDetail({ kind: "error" });
      });
    return () => controller.abort();
  }, [expanded, detail.kind, row.eventIds]);

  return (
    <div
      className="row row--busy"
      style={{ borderLeftColor: color }}
      onPointerDown={expanded ? resetIdle : undefined}
    >
      <div className="row__member">
        <MemberTag member={member} fallbackId={row.memberId} />
      </div>
      <button
        type="button"
        className="busy"
        aria-expanded={expanded}
        aria-controls={panelId}
        onClick={() => setExpanded((v) => !v)}
      >
        <span className="busy__track" aria-hidden="true">
          <span
            className="busy__fill"
            style={{
              left: `${geometry.top * 100}%`,
              width: `${geometry.height * 100}%`,
              background: withAlpha(color, 0.7),
              borderLeft: `5px solid ${color}`,
            }}
          />
        </span>
        <span className="busy__label">busy {formatTimeRange(row.startLocal, row.endLocal)}</span>
        <span className="busy__count">{countLabel}</span>
        <span className="busy__chevron" aria-hidden="true">
          {expanded ? "▾" : "▸"}
        </span>
      </button>

      {expanded ? (
        <div className="busy__panel" id={panelId}>
          {detail.kind === "loading" ? <p className="busy__note">Loading {countLabel}…</p> : null}
          {detail.kind === "error" ? (
            <p className="busy__note">
              {countLabel} — details unavailable right now. Nothing has been hidden.
            </p>
          ) : null}
          {detail.kind === "ready" ? (
            detail.events.length > 0 ? (
              <ul className="busy__list">
                {detail.events.map((event) => (
                  <li key={event.eventId} className="busy__item">
                    <span className="busy__item-time">
                      {event.startLocal === undefined ? "" : formatTime(event.startLocal)}
                    </span>
                    <span className="busy__item-title">{event.title}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="busy__note">{countLabel} — no titles available.</p>
            )
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
