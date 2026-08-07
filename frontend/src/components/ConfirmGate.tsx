import { useEffect, useId, useRef } from "react";

import { confirmLabels, isDestructive } from "../lib/agent";
import type { Member, PendingConfirmation } from "../types";
import { MemberTag } from "./MemberTag";

interface ConfirmGateProps {
  pending: PendingConfirmation;
  /** Whose authority the write would carry. */
  actor: Member | undefined;
  actorId: string;
  /** A turn is already in flight; both answers are locked out until it lands. */
  busy: boolean;
  onAnswer: (approved: boolean) => void;
}

/**
 * The server stopped and is waiting for a human.
 *
 * Three things this component is not allowed to do, all from the contract:
 * it never auto-answers, it never decides the gate was unnecessary, and it
 * never sends the answer anywhere except back through a new turn.
 *
 * The destructive layout is deliberately awkward. The affirmative sits small
 * and outlined in the top-right corner; the full-width filled button along the
 * bottom - where a thumb rests on a wall-mounted panel - is the one that keeps
 * the event. Hitting this by accident should be hard.
 */
export function ConfirmGate({ pending, actor, actorId, busy, onAnswer }: ConfirmGateProps) {
  const summaryId = useId();
  const danger = isDestructive(pending.tool);
  const labels = confirmLabels(pending);
  const safeRef = useRef<HTMLButtonElement>(null);

  // Focus lands on the safe answer, never the affirmative: a stray Enter from
  // the keyboard the user was just typing on must not approve a delete.
  useEffect(() => {
    safeRef.current?.focus();
  }, [pending.callId]);

  return (
    <section
      className={`gate${danger ? " gate--danger" : ""}`}
      role="alertdialog"
      aria-labelledby={summaryId}
    >
      <p className="gate__eyebrow">{danger ? "Confirm — this deletes" : "Confirm"}</p>
      <p className="gate__summary" id={summaryId}>
        {pending.summary}
      </p>
      <p className="gate__actor">
        <span className="gate__actor-label">Acting as</span>
        <MemberTag member={actor} fallbackId={actorId} />
      </p>

      <div className={`gate__actions${danger ? " gate__actions--danger" : ""}`}>
        <button
          type="button"
          ref={safeRef}
          className="gate__btn gate__btn--safe"
          disabled={busy}
          onClick={() => onAnswer(false)}
        >
          {labels.reject}
        </button>
        <button
          type="button"
          className={`gate__btn ${danger ? "gate__btn--danger" : "gate__btn--approve"}`}
          disabled={busy}
          onClick={() => onAnswer(true)}
        >
          {labels.approve}
        </button>
      </div>
    </section>
  );
}
