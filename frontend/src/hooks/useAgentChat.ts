import { useCallback, useRef, useState } from "react";

import { postAgentTurn } from "../api";
import { confirmMessage, hasAppliedWrite } from "../lib/agent";
import type { AgentAction, AgentConfirmAnswer, AgentTurnRequest, PendingConfirmation } from "../types";

/**
 * One line of the on-screen transcript. `error` is a line, not a screen: a
 * failed turn must never take the calendar behind the panel down with it.
 */
export type ChatEntry =
  | { id: string; role: "you"; text: string }
  | { id: string; role: "agent"; text: string; actions: readonly AgentAction[] }
  | { id: string; role: "error"; text: string };

export interface AgentChat {
  entries: readonly ChatEntry[];
  /** The server's open gate. Answering it is the *only* way past it. */
  pending: PendingConfirmation | null;
  /** A turn is in flight. Opus 5 thinks before it answers; this can be seconds. */
  busy: boolean;
  send: (text: string) => void;
  answer: (approved: boolean) => void;
}

interface Options {
  /**
   * Household zone, straight off the agenda envelope. The browser never
   * computes with it - it is a string we forward so the server knows which
   * wall clock "thursday at 4" means.
   */
  tz: string;
  /** Called after a write actually landed, so the day view can re-fetch. */
  onWrite: () => void;
}

/** `crypto.randomUUID` needs a secure context; a LAN kiosk on http has none. */
let seq = 0;
function nextId(): string {
  seq += 1;
  return `e${seq}`;
}

function browserZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone;
  } catch {
    return "UTC";
  }
}

export function useAgentChat({ tz, onWrite }: Options): AgentChat {
  const [entries, setEntries] = useState<readonly ChatEntry[]>([]);
  const [pending, setPending] = useState<PendingConfirmation | null>(null);
  const [busy, setBusy] = useState(false);

  // Refs, not state: a turn already in flight must read the *current*
  // conversation id, and a second send must not race the first.
  const conversationId = useRef<string | null>(null);
  const inFlight = useRef(false);

  const runTurn = useCallback(
    (message: string, confirm?: AgentConfirmAnswer) => {
      if (inFlight.current) return;
      inFlight.current = true;
      setBusy(true);
      setEntries((prev) => [...prev, { id: nextId(), role: "you", text: message }]);
      // The gate closes the moment it is answered, so a second tap on a stale
      // button cannot send the answer twice.
      setPending(null);

      const request: AgentTurnRequest = {
        message,
        // An instant, not wall-clock arithmetic. The server places it in the
        // user turn; the browser's only job is to read its own clock.
        now: new Date().toISOString(),
        tz: tz.length > 0 ? tz : browserZone(),
      };
      if (conversationId.current !== null) request.conversationId = conversationId.current;
      if (confirm !== undefined) request.confirm = confirm;

      const run = async () => {
        try {
          const turn = await postAgentTurn(request);
          conversationId.current = turn.conversationId;
          setEntries((prev) =>
            turn.reply.length > 0
              ? [...prev, { id: nextId(), role: "agent", text: turn.reply, actions: turn.actions }]
              : prev,
          );
          if (turn.pendingConfirmation !== undefined) setPending(turn.pendingConfirmation);
          // Re-fetch rather than splice in a row we made up: the point of the
          // whole feature is that the screen shows what the calendar holds.
          if (hasAppliedWrite(turn.actions)) onWrite();
        } catch (err) {
          const text = err instanceof Error ? err.message : "Could not reach the assistant";
          setEntries((prev) => [...prev, { id: nextId(), role: "error", text }]);
        } finally {
          inFlight.current = false;
          setBusy(false);
        }
      };
      void run();
    },
    [tz, onWrite],
  );

  const send = useCallback(
    (text: string) => {
      const message = text.trim();
      if (message.length === 0) return;
      runTurn(message);
    },
    [runTurn],
  );

  /**
   * The answer to a gate goes back as a new turn carrying `confirm`. Both
   * answers make the round-trip: "no" is a decision the server has to record
   * and the agent has to hear, not something the UI resolves on its own.
   */
  const answer = useCallback(
    (approved: boolean) => {
      if (pending === null) return;
      runTurn(confirmMessage(approved), { callId: pending.callId, approved });
    },
    [pending, runTurn],
  );

  return { entries, pending, busy, send, answer };
}
