import type { RefObject } from "react";

export type ViewMode = "day" | "week";

interface NavBarProps {
  view: ViewMode;
  /** True when the day view is showing today / tomorrow, for the pressed state. */
  isToday: boolean;
  isTomorrow: boolean;
  /** The chat panel is open, for the ⌨ button's expanded state. */
  chatOpen: boolean;
  onPrev: () => void;
  onNext: () => void;
  onToday: () => void;
  onTomorrow: () => void;
  onWeek: () => void;
  onChat: () => void;
  /** So focus can come back here when the panel closes. */
  chatRef?: RefObject<HTMLButtonElement | null>;
}

/**
 * Finger-sized targets, not mouse-sized: every control here is at least 4.5rem
 * tall with generous horizontal padding, because this gets tapped by someone
 * holding a coffee.
 */
export function NavBar({
  view,
  isToday,
  isTomorrow,
  chatOpen,
  onPrev,
  onNext,
  onToday,
  onTomorrow,
  onWeek,
  onChat,
  chatRef,
}: NavBarProps) {
  const dayView = view === "day";

  return (
    <nav className="nav" aria-label="Day navigation">
      <button type="button" className="nav__arrow" onClick={onPrev} aria-label="Previous">
        <span aria-hidden="true">◀</span>
      </button>

      <div className="nav__group">
        <button
          type="button"
          className="nav__btn"
          aria-pressed={dayView && isToday}
          onClick={onToday}
        >
          Today
        </button>
        <button
          type="button"
          className="nav__btn"
          aria-pressed={dayView && isTomorrow}
          onClick={onTomorrow}
        >
          Tomorrow
        </button>
        <button
          type="button"
          className="nav__btn"
          aria-pressed={view === "week"}
          onClick={onWeek}
        >
          Week
        </button>
      </div>

      <div className="nav__group">
        {/* PRD §11 puts ⌨ and 🎤 side by side here. Only ⌨ is real in M2; a mic
            button that did nothing would be a promise the screen can't keep. */}
        <button
          type="button"
          className="nav__arrow nav__arrow--chat"
          ref={chatRef ?? null}
          onClick={onChat}
          aria-expanded={chatOpen}
          aria-label="Ask the calendar"
        >
          <span aria-hidden="true">⌨</span>
        </button>
        <button type="button" className="nav__arrow" onClick={onNext} aria-label="Next">
          <span aria-hidden="true">▶</span>
        </button>
      </div>
    </nav>
  );
}
