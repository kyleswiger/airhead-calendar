import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent as ReactKeyboardEvent } from "react";

import type { RoutinesState } from "../hooks/useRoutines";
import type { MemberIndex } from "../lib/agenda";
import { dueLabel, intervalLabel, lastDoneLabel, statusChip } from "../lib/routines";
import type { CreateRoutineBody, EstimateResponse, Routine } from "../types";
import { MemberTag } from "./MemberTag";

interface RoutinesPanelProps {
  routines: RoutinesState;
  members: MemberIndex;
  today: string;
  onClose: () => void;
}

const FOCUSABLE = "button:not([disabled]), input:not([disabled]), [href]";

/**
 * The routines list as a mode over the day view, same shape as ChatPanel: the
 * right-hand third, the calendar still lit behind it, Escape and ✕ both
 * return, focus stays inside. Nothing here computes a due date - every label
 * restates a field the server sent.
 */
export function RoutinesPanel({ routines, members, today, onClose }: RoutinesPanelProps) {
  const { routines: list, status, error, busyId, recent } = routines;
  const panelRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);

  // Fresh data on open: the last refresh could be five minutes old.
  const reload = routines.reload;
  useEffect(() => {
    reload();
    closeRef.current?.focus();
  }, [reload]);

  useEffect(() => {
    const onEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onEscape);
    return () => document.removeEventListener("keydown", onEscape);
  }, [onClose]);

  // Focus must never fall out of the panel: a disabled Done button or an
  // unmounted Undo drops focus on <body>, and the keyboard goes dead.
  useEffect(() => {
    const panel = panelRef.current;
    if (panel === null) return;
    const active = document.activeElement;
    if (active !== null && active !== document.body && panel.contains(active)) return;
    closeRef.current?.focus();
  }, [busyId, recent, list]);

  const onKeyDown = (event: ReactKeyboardEvent<HTMLElement>) => {
    if (event.key !== "Tab") return;
    const panel = panelRef.current;
    if (panel === null) return;
    const items = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE));
    const first = items[0];
    const last = items[items.length - 1];
    if (first === undefined || last === undefined) return;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <>
      <div className="chat-scrim" onClick={onClose} aria-hidden="true" />
      <aside
        className="chat routines"
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label="Routines"
        onKeyDown={onKeyDown}
      >
        <header className="chat__head">
          <div className="chat__ident">
            <h2 className="chat__title">Routines</h2>
            <p className="routines__sub">Things done every so often. Tap Done; the next date lands on the calendar.</p>
          </div>
          <button
            type="button"
            className="chat__close"
            ref={closeRef}
            onClick={onClose}
            aria-label="Close routines"
          >
            <span aria-hidden="true">✕</span>
          </button>
        </header>

        <div className="routines__body">
          {error !== null ? (
            <p className="routines__error" role="alert">
              {error}
              <button type="button" className="routines__link" onClick={routines.clearError}>
                Dismiss
              </button>
            </p>
          ) : null}

          {recent !== null ? (
            <p className="routines__undo" role="status">
              <span>✓ “{recent.routineName}” marked done today.</span>
              {recent.completionId !== null ? (
                <button
                  type="button"
                  className="routines__undo-btn"
                  disabled={busyId !== null}
                  onClick={() => void routines.undo()}
                >
                  Undo
                </button>
              ) : null}
            </p>
          ) : null}

          {status === "loading" && list.length === 0 ? (
            <p className="routines__note">Loading…</p>
          ) : status === "error" && list.length === 0 ? (
            <p className="routines__note">Can’t reach the routines list.</p>
          ) : list.length === 0 ? (
            <p className="routines__note">
              Nothing here yet. A routine is anything you do every so often — a haircut, the
              furnace filter, the gutters — and the calendar remembers when it’s next due.
            </p>
          ) : (
            <ul className="rtn-list">
              {list.map((routine) => (
                <RoutineItem
                  key={routine.routineId}
                  routine={routine}
                  members={members}
                  today={today}
                  busy={busyId === routine.routineId}
                  anyBusy={busyId !== null}
                  expanded={expanded === routine.routineId}
                  onToggle={() =>
                    setExpanded((cur) => (cur === routine.routineId ? null : routine.routineId))
                  }
                  onDone={() => void routines.complete(routine.routineId)}
                  onPause={(paused) => void routines.patch(routine.routineId, { paused })}
                  onRemove={() => void routines.remove(routine.routineId)}
                />
              ))}
            </ul>
          )}

          {adding ? (
            <AddForm
              today={today}
              members={members}
              onEstimate={routines.estimate}
              onCreate={async (body) => {
                const created = await routines.create(body);
                if (created !== null) setAdding(false);
              }}
              onCancel={() => setAdding(false)}
            />
          ) : (
            <button type="button" className="rtn-add__open" onClick={() => setAdding(true)}>
              <span aria-hidden="true">＋ </span>Add a routine
            </button>
          )}
        </div>
      </aside>
    </>
  );
}

/* ---------------------------------------------------------------- row -- */

interface RoutineItemProps {
  routine: Routine;
  members: MemberIndex;
  today: string;
  busy: boolean;
  anyBusy: boolean;
  expanded: boolean;
  onToggle: () => void;
  onDone: () => void;
  onPause: (paused: boolean) => void;
  onRemove: () => void;
}

function RoutineItem({
  routine,
  members,
  today,
  busy,
  anyBusy,
  expanded,
  onToggle,
  onDone,
  onPause,
  onRemove,
}: RoutineItemProps) {
  const chip = statusChip(routine);
  const interval = intervalLabel(routine);
  const due = dueLabel(routine);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const detailId = `rtn-detail-${routine.routineId}`;

  return (
    <li className={`rtn rtn--${routine.status.replace("_", "-")}`}>
      <button
        type="button"
        className="rtn__main"
        onClick={onToggle}
        aria-expanded={expanded}
        aria-controls={detailId}
      >
        <span className="rtn__headline">
          {chip !== null ? <span className={`chip chip--${chip.kind}`}>{chip.text}</span> : null}
          {routine.intervalSource === "estimated" ? (
            <span className="chip chip--est" title="Interval is an estimate">
              est.
            </span>
          ) : null}
          <span className="rtn__name">{routine.name}</span>
        </span>
        <span className="rtn__meta">
          <span className="rtn__who">
            {routine.memberIds.map((id) => (
              <MemberTag key={id} member={members.get(id)} fallbackId={id} size="small" />
            ))}
          </span>
          <span className="rtn__when">
            {lastDoneLabel(routine.lastDoneOn, today)}
            {interval !== null ? ` · ${interval}` : ""}
            {due !== null && routine.status !== "paused" ? ` · ${due}` : ""}
          </span>
        </span>
      </button>

      <button
        type="button"
        className="rtn__done"
        disabled={anyBusy}
        aria-busy={busy}
        aria-label={`Mark ${routine.name} done today`}
        onClick={onDone}
      >
        Done today
      </button>

      {expanded ? (
        <div className="rtn__detail" id={detailId}>
          {routine.intervalNote !== null ? (
            <p className="rtn__note">
              {routine.intervalNote}
              {routine.intervalConfidence !== null
                ? ` (confidence ${Math.round(routine.intervalConfidence * 100)}%)`
                : ""}
            </p>
          ) : routine.intervalDays === null ? (
            <p className="rtn__note">No interval yet — ask the calendar how often, or edit it.</p>
          ) : (
            <p className="rtn__note">
              Interval set by{" "}
              {routine.intervalSource === "human"
                ? "you"
                : routine.intervalSource === "observed"
                  ? "how often it actually gets done"
                  : "the catalog"}
              .
            </p>
          )}
          {routine.dueOn !== null ? <p className="rtn__note">Next due {routine.dueOn}.</p> : null}
          <div className="rtn__actions">
            <button
              type="button"
              className="rtn__action"
              disabled={anyBusy}
              onClick={() => onPause(!routine.paused)}
            >
              {routine.paused ? "Resume" : "Pause"}
            </button>
            {confirmRemove ? (
              <>
                <button
                  type="button"
                  className="rtn__action rtn__action--danger"
                  disabled={anyBusy}
                  onClick={onRemove}
                >
                  Yes, remove
                </button>
                <button type="button" className="rtn__action" onClick={() => setConfirmRemove(false)}>
                  Keep it
                </button>
              </>
            ) : (
              <button
                type="button"
                className="rtn__action"
                disabled={anyBusy}
                onClick={() => setConfirmRemove(true)}
              >
                Remove…
              </button>
            )}
          </div>
        </div>
      ) : null}
    </li>
  );
}

/* --------------------------------------------------------------- add form -- */

interface AddFormProps {
  today: string;
  members: MemberIndex;
  onEstimate: (name: string, context?: string) => Promise<EstimateResponse>;
  onCreate: (body: CreateRoutineBody) => Promise<void>;
  onCancel: () => void;
}

function AddForm({ today, onEstimate, onCreate, onCancel }: AddFormProps) {
  const [name, setName] = useState("");
  const [every, setEvery] = useState("");
  const [lastDone, setLastDone] = useState(today);
  const [estimate, setEstimate] = useState<EstimateResponse | null>(null);
  const [estimating, setEstimating] = useState(false);
  const [saving, setSaving] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const nameRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    nameRef.current?.focus();
  }, []);

  const trimmed = name.trim();
  const everyDays = every.trim().length === 0 ? null : Number(every);
  const everyValid = everyDays === null || (Number.isInteger(everyDays) && everyDays > 0);

  const suggest = async () => {
    if (trimmed.length === 0 || estimating) return;
    setEstimating(true);
    setProblem(null);
    try {
      const result = await onEstimate(trimmed);
      setEstimate(result);
      if (result.intervalDays !== null) setEvery(String(result.intervalDays));
    } catch (err) {
      setProblem(err instanceof Error ? err.message : "Couldn’t get a suggestion");
    } finally {
      setEstimating(false);
    }
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (trimmed.length === 0 || !everyValid || saving) return;
    setSaving(true);
    setProblem(null);
    const body: CreateRoutineBody = { name: trimmed };
    if (lastDone.length > 0) body.lastDoneOn = lastDone;
    if (everyDays !== null) {
      body.intervalDays = everyDays;
      // Untouched suggestion ⇒ pass the estimate through with its rationale so
      // the row can say "est."; a number the person typed is theirs (`human`).
      if (estimate !== null && estimate.intervalDays === everyDays) {
        body.intervalSource = estimate.source;
        if (estimate.rationale.length > 0) body.intervalNote = estimate.rationale;
        if (estimate.confidence !== null) body.intervalConfidence = estimate.confidence;
        if (estimate.category !== null) body.category = estimate.category;
        body.anchor = estimate.anchor;
      }
    }
    await onCreate(body);
    setSaving(false);
  };

  return (
    <form className="rtn-add" onSubmit={(e) => void submit(e)} aria-label="Add a routine">
      <label className="rtn-add__label" htmlFor="rtn-name">
        What is it?
      </label>
      <input
        id="rtn-name"
        ref={nameRef}
        className="rtn-add__input"
        type="text"
        value={name}
        placeholder="Cabin air filter"
        autoComplete="off"
        onChange={(e) => {
          setName(e.target.value);
          setEstimate(null);
        }}
      />

      <div className="rtn-add__row">
        <label className="rtn-add__label" htmlFor="rtn-every">
          Every
        </label>
        <input
          id="rtn-every"
          className="rtn-add__input rtn-add__input--num"
          type="number"
          inputMode="numeric"
          min={1}
          step={1}
          value={every}
          placeholder="—"
          aria-invalid={!everyValid}
          onChange={(e) => setEvery(e.target.value)}
        />
        <span className="rtn-add__unit">days</span>
        <button
          type="button"
          className="rtn-add__suggest"
          disabled={trimmed.length === 0 || estimating}
          aria-busy={estimating}
          onClick={() => void suggest()}
        >
          {estimating ? "Thinking…" : "Suggest"}
        </button>
      </div>

      {estimate !== null ? (
        <div className="rtn-add__estimate" role="status">
          {estimate.intervalDays !== null ? (
            <p className="rtn-add__rationale">
              <span className={`chip chip--${estimate.source === "catalog" ? "catalog" : "est"}`}>
                {estimate.source === "catalog" ? "catalog" : "est."}
              </span>{" "}
              {estimate.rationale.length > 0 ? estimate.rationale : `About every ${estimate.intervalDays} days.`}
            </p>
          ) : (
            <p className="rtn-add__rationale">No suggestion — enter a number if you know one.</p>
          )}
          {estimate.followUpQuestion !== null ? (
            <p className="rtn-add__followup">{estimate.followUpQuestion}</p>
          ) : null}
        </div>
      ) : null}

      <div className="rtn-add__row">
        <label className="rtn-add__label" htmlFor="rtn-last">
          Last done
        </label>
        <input
          id="rtn-last"
          className="rtn-add__input rtn-add__input--date"
          type="date"
          max={today}
          value={lastDone}
          onChange={(e) => setLastDone(e.target.value)}
        />
      </div>

      {problem !== null ? (
        <p className="routines__error" role="alert">
          {problem}
        </p>
      ) : null}

      <div className="rtn-add__actions">
        <button type="button" className="rtn__action" onClick={onCancel}>
          Cancel
        </button>
        <button
          type="submit"
          className="rtn-add__save"
          disabled={trimmed.length === 0 || !everyValid || saving}
        >
          {saving ? "Adding…" : "Add"}
        </button>
      </div>
    </form>
  );
}
