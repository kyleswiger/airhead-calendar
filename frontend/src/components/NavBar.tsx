export type ViewMode = "day" | "week";

interface NavBarProps {
  view: ViewMode;
  /** True when the day view is showing today / tomorrow, for the pressed state. */
  isToday: boolean;
  isTomorrow: boolean;
  onPrev: () => void;
  onNext: () => void;
  onToday: () => void;
  onTomorrow: () => void;
  onWeek: () => void;
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
  onPrev,
  onNext,
  onToday,
  onTomorrow,
  onWeek,
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

      <button type="button" className="nav__arrow" onClick={onNext} aria-label="Next">
        <span aria-hidden="true">▶</span>
      </button>
    </nav>
  );
}
