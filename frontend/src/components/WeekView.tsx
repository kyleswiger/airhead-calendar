import { barGeometry, busyRows, compareEvents, eventRows, type MemberIndex } from "../lib/agenda";
import { safeColor, withAlpha } from "../lib/color";
import { dayOfMonth, formatTime, formatWeekdayShort, pluralize } from "../lib/format";
import type { AgendaDay } from "../types";
import { MemberTag } from "./MemberTag";

interface WeekViewProps {
  days: AgendaDay[];
  members: MemberIndex;
  today: string;
}

/**
 * Seven columns, same tier logic as the day view.
 *
 * PRD §11: T3 is rendered as bar height only, no text - at week scale the
 * shape of the week is the message. The count still travels in the bar's
 * accessible name, and the day view remains the place the number is legible,
 * so nothing is actually hidden from anyone.
 */
export function WeekView({ days, members, today }: WeekViewProps) {
  return (
    <section className="week" aria-label="Week view">
      {days.map((day) => {
        const bands = busyRows(day);
        const events = eventRows(day).sort(compareEvents);

        return (
          <div
            key={day.date}
            className={`week__col${day.date === today ? " week__col--today" : ""}`}
          >
            <h3 className="week__head">
              <span className="week__weekday">{formatWeekdayShort(day.date)}</span>
              <span className="week__daynum">{dayOfMonth(day.date)}</span>
            </h3>

            <div className="week__busy" aria-hidden={bands.length === 0}>
              {bands.map((band) => {
                const member = members.get(band.memberId);
                const color = safeColor(member?.color);
                const geometry = barGeometry(band.startLocal, band.endLocal);
                const name = member?.displayName ?? band.memberId;
                return (
                  <div
                    key={band.memberId}
                    className="week__bar-track"
                    role="img"
                    aria-label={`${name} busy, ${pluralize(band.count, "meeting")}`}
                    title={`${name} — ${pluralize(band.count, "meeting")}`}
                  >
                    <div
                      className="week__bar"
                      style={{
                        top: `${geometry.top * 100}%`,
                        height: `${geometry.height * 100}%`,
                        background: withAlpha(color, 0.6),
                        borderTop: `3px solid ${color}`,
                      }}
                    />
                  </div>
                );
              })}
            </div>

            <ul className="week__events">
              {events.length === 0 && bands.length === 0 ? (
                <li className="week__free">Free</li>
              ) : null}
              {events.map((event) => (
                <li
                  key={event.occurrenceId ?? event.eventId}
                  className={`week__event week__event--${event.tier.toLowerCase()}`}
                  style={{ borderLeftColor: safeColor(members.get(event.ownerMemberId)?.color) }}
                >
                  <span className="week__event-when">
                    {event.allDay ? "All day" : formatTime(event.startLocal)}
                  </span>
                  <span className="week__event-title">{event.title}</span>
                  {/* `memberIds` is already roster-ordered, so the names come
                      out in the same order as the colors everywhere else. At
                      week scale every involved name shows instead of a FAMILY
                      chip - a chip small enough to fit would fall under the
                      28px floor, and the names say more anyway. */}
                  <span className="week__event-who">
                    {event.memberIds.map((id) => (
                      <MemberTag key={id} member={members.get(id)} fallbackId={id} size="small" />
                    ))}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        );
      })}
    </section>
  );
}
