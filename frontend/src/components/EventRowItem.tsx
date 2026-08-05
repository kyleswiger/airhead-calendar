import { safeColor } from "../lib/color";
import { memberNames, type MemberIndex } from "../lib/agenda";
import { formatMonthDay, formatTime } from "../lib/format";
import type { EventRow } from "../types";
import { MemberTag } from "./MemberTag";

interface EventRowItemProps {
  event: EventRow;
  members: MemberIndex;
  /** The day this row is being rendered under, `YYYY-MM-DD`. */
  onDate: string;
}

/**
 * A T1 or T2 row: who / when / what.
 *
 * T1 renders at full strength; T2 is dimmed but never removed - it is a real
 * commitment that just doesn't obligate anyone else (PRD §6.1). T3 never
 * reaches this component; it arrives as a `busy` band instead.
 */
export function EventRowItem({ event, members, onDate }: EventRowItemProps) {
  const owner = members.get(event.ownerMemberId);
  const accent = safeColor(owner?.color);
  const involved = event.memberIds.length > 0 ? event.memberIds : [event.ownerMemberId];
  const others = involved.filter((id) => id !== event.ownerMemberId);

  // A multi-day event repeats on every day it covers, carrying its full span
  // each time. Without this, day two of a red-eye reads "10:00 PM" - yesterday's
  // departure, printed under today's date, with nothing saying so.
  // Comparing date prefixes of two floating local strings is not timezone math.
  // A row with no end is a malformed row the parser let through rather than
  // dropped; it can still show a start time, just not a span.
  const endLocal = event.allDay ? undefined : event.endLocal;
  const startsBefore =
    !event.allDay && endLocal !== undefined && event.startLocal.slice(0, 10) < onDate;
  const endsAfter = endLocal !== undefined && endLocal.slice(0, 10) > onDate;

  return (
    <div
      className={`row row--event row--${event.tier.toLowerCase()}`}
      style={{ borderLeftColor: accent }}
    >
      <div className="row__member">
        <MemberTag member={owner} fallbackId={event.ownerMemberId} />
      </div>

      {/* The `allDay` discriminant decides which format `startLocal` is in:
          a bare date, or a floating local datetime. Never both. */}
      <div className="row__time">
        {event.allDay ? (
          <span className="row__allday">
            All day
            {/* endLocal is the INCLUSIVE last covered day - printed as given,
                never nudged by a day's arithmetic. */}
            {event.endLocal !== event.startLocal ? (
              <span className="row__through">
                <span aria-hidden="true">→ </span>
                <span className="sr-only">through </span>
                {formatMonthDay(event.endLocal)}
              </span>
            ) : null}
          </span>
        ) : startsBefore && endLocal !== undefined ? (
          // No "until" - the time column is 8.5rem and at 28px the extra word
          // overflows under the title, putting the FAMILY chip on top of it.
          // The arrow carries the meaning; the screen reader gets the words.
          <span className="row__continues">
            <span aria-hidden="true">↳ </span>
            <span className="sr-only">continues from yesterday, ends at </span>
            {formatTime(endLocal)}
          </span>
        ) : (
          <span>
            {formatTime(event.startLocal)}
            {endsAfter ? (
              <>
                <span aria-hidden="true"> →</span>
                <span className="sr-only"> continues into the next day</span>
              </>
            ) : null}
          </span>
        )}
      </div>

      <div className="row__body">
        <div className="row__headline">
          {event.isFamily ? <span className="chip chip--family">Family</span> : null}
          <span className="row__title">{event.title}</span>
        </div>
        {others.length > 0 || event.location !== undefined ? (
          <div className="row__meta">
            {others.length > 0 ? (
              <span className="row__with">
                with{" "}
                {memberNames(members, others).map((name, i) => (
                  <span key={name}>
                    {i > 0 ? ", " : ""}
                    {name}
                  </span>
                ))}
              </span>
            ) : null}
            {event.location !== undefined ? (
              <span className="row__location">{event.location}</span>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}
