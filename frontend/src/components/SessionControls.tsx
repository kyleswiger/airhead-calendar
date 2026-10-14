import { useEffect, useState } from "react";

import type { Member } from "../types";
import { MemberTag } from "./MemberTag";

interface SessionControlsProps {
  members: readonly Member[];
  actorId: string;
  onActorChange: (memberId: string) => void;
  /** Absent in fixture mode, where there is nothing to leave. */
  exitLabel?: string;
  onExit?: () => void;
}

const CONFIRM_MS = 4000;

/**
 * Who is acting (tap to hand the screen to the next person) and the way out.
 *
 * The exit takes two taps: on a wall panel a stray elbow must not sign the
 * kitchen out, and a `confirm()` dialog is a worse thing to find on a kiosk.
 */
export function SessionControls({ members, actorId, onActorChange, exitLabel, onExit }: SessionControlsProps) {
  const [arming, setArming] = useState(false);

  useEffect(() => {
    if (!arming) return;
    const timer = window.setTimeout(() => setArming(false), CONFIRM_MS);
    return () => window.clearTimeout(timer);
  }, [arming]);

  const actor = members.find((m) => m.memberId === actorId);
  const next = () => {
    if (members.length < 2) return;
    const index = members.findIndex((m) => m.memberId === actorId);
    const following = members[(index + 1) % members.length];
    if (following !== undefined) onActorChange(following.memberId);
  };

  return (
    <div className="session">
      <button
        type="button"
        className="session__btn"
        onClick={next}
        disabled={members.length < 2}
        aria-label={`Acting as ${actor?.displayName ?? actorId}. Tap to switch person.`}
      >
        <MemberTag member={actor} fallbackId={actorId} size="small" />
      </button>
      {onExit !== undefined && exitLabel !== undefined ? (
        <button
          type="button"
          className={`session__btn${arming ? " session__btn--armed" : ""}`}
          onClick={() => {
            if (arming) onExit();
            else setArming(true);
          }}
        >
          {arming ? "Tap again" : exitLabel}
        </button>
      ) : null}
    </div>
  );
}
