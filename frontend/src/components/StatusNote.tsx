import { formatAge } from "../lib/format";

interface StatusNoteProps {
  /** Rendering the bundled sample rather than live household data. */
  fixture: boolean;
  /** Last attempt failed but there is still good data on screen. */
  stale: boolean;
  fetchedAt: number | null;
  now: Date;
}

/**
 * A one-line truth marker, never a modal and never an error page.
 *
 * When the network drops, the household still wants to see this morning's
 * agenda - so the last good payload stays up and this line says how old it is.
 * Silence here means the screen is live.
 */
export function StatusNote({ fixture, stale, fetchedAt, now }: StatusNoteProps) {
  if (fixture) {
    return (
      <p className="status status--sample" role="status">
        Sample data — no calendar connected yet
      </p>
    );
  }
  if (!stale) return null;
  const age = fetchedAt === null ? "unknown age" : formatAge(now.getTime() - fetchedAt);
  return (
    <p className="status status--stale" role="status">
      Offline — showing last update from {age}
    </p>
  );
}
