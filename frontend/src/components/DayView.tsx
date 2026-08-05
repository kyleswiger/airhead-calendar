import { orderedRows, type MemberIndex } from "../lib/agenda";
import { formatMonthDayLong, formatWeekdayLong } from "../lib/format";
import type { AgendaDay } from "../types";
import { isBusyRow } from "../types";
import { BusyBand } from "./BusyBand";
import { EventRowItem } from "./EventRowItem";

interface DayViewProps {
  day: AgendaDay;
  members: MemberIndex;
  /** Shown above the list when the selected day isn't today. */
  heading?: string;
}

export function DayView({ day, members, heading }: DayViewProps) {
  const rows = orderedRows(day, members);

  return (
    <section className="day" aria-label={`${formatWeekdayLong(day.date)} ${formatMonthDayLong(day.date)}`}>
      {heading !== undefined ? <h2 className="day__heading">{heading}</h2> : null}
      {rows.length === 0 ? (
        <p className="day__empty">Nothing scheduled.</p>
      ) : (
        <div className="day__rows">
          {rows.map((row) =>
            isBusyRow(row) ? (
              <BusyBand
                key={`busy-${row.memberId}-${row.startLocal}`}
                row={row}
                member={members.get(row.memberId)}
              />
            ) : (
              <EventRowItem
                key={row.occurrenceId ?? row.eventId}
                event={row}
                members={members}
                onDate={day.date}
              />
            ),
          )}
        </div>
      )}
    </section>
  );
}
