import { useCallback, useEffect, useRef, useState } from "react";

import {
  completeRoutine,
  createRoutine,
  deleteRoutine,
  estimateRoutine,
  listCompletions,
  listRoutines,
  patchRoutine,
  undoCompletion,
} from "../api";
import { todayIsoDate } from "../lib/format";
import { orderRoutines } from "../lib/routines";
import type { CreateRoutineBody, EstimateResponse, Routine, RoutinePatch } from "../types";

const REFRESH_MS = 5 * 60_000;
/** How long the Undo stays offered after a tap. Mis-taps are normal; regret is short. */
export const UNDO_WINDOW_MS = 10_000;

export type RoutinesStatus = "loading" | "ready" | "error";

/** The completion just recorded, while its Undo is still on offer. */
export interface RecentCompletion {
  routineId: string;
  routineName: string;
  /** Null when the completion could not be identified - then there is no Undo. */
  completionId: string | null;
  /** Epoch ms after which the offer is withdrawn. */
  until: number;
}

export interface RoutinesState {
  routines: readonly Routine[];
  status: RoutinesStatus;
  /** The last mutation or load failure, one line. Cleared by `clearError`. */
  error: string | null;
  /** The routine a mutation is in flight for, so its button can disable. */
  busyId: string | null;
  recent: RecentCompletion | null;
  reload: () => void;
  complete: (routineId: string) => Promise<boolean>;
  undo: () => Promise<boolean>;
  create: (body: CreateRoutineBody) => Promise<Routine | null>;
  patch: (routineId: string, patch: RoutinePatch) => Promise<Routine | null>;
  remove: (routineId: string) => Promise<boolean>;
  /** Throws on failure so the form can show the message next to the button. */
  estimate: (name: string, context?: string) => Promise<EstimateResponse>;
  clearError: () => void;
}

interface Options {
  /**
   * Called after any write that moves a due event, so the agenda can re-fetch.
   * The routine list itself is reloaded here; the agenda is the caller's.
   */
  onWrite?: () => void;
}

function describe(err: unknown, fallback: string): string {
  return err instanceof Error && err.message.length > 0 ? err.message : fallback;
}

/**
 * The routines list and its mutations.
 *
 * Every write re-fetches the list afterwards rather than trusting the local
 * patch: the server re-projects the due event and re-derives the interval
 * (observed rule) on each completion, and the display must show what it holds.
 * The optimistic update only covers the moment between tap and reply, so a
 * "Done today" tap reads as done instantly on a slow connection.
 */
export function useRoutines({ onWrite }: Options = {}): RoutinesState {
  const [routines, setRoutines] = useState<readonly Routine[]>([]);
  const [status, setStatus] = useState<RoutinesStatus>("loading");
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [recent, setRecent] = useState<RecentCompletion | null>(null);
  const [nonce, setNonce] = useState(0);
  const hasData = useRef(false);
  const undoTimer = useRef<number | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;
    const run = async () => {
      try {
        const next = await listRoutines(controller.signal);
        if (cancelled) return;
        hasData.current = true;
        setRoutines(orderRoutines(next));
        setStatus("ready");
      } catch (err) {
        if (cancelled || controller.signal.aborted) return;
        setError(describe(err, "Could not load routines"));
        setStatus(hasData.current ? "ready" : "error");
      }
    };
    void run();
    const timer = window.setInterval(() => void run(), REFRESH_MS);
    return () => {
      cancelled = true;
      controller.abort();
      window.clearInterval(timer);
    };
  }, [nonce]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  const clearError = useCallback(() => setError(null), []);

  const afterWrite = useCallback(() => {
    reload();
    onWrite?.();
  }, [reload, onWrite]);

  const replace = useCallback((routine: Routine) => {
    setRoutines((prev) =>
      orderRoutines(prev.map((r) => (r.routineId === routine.routineId ? routine : r))),
    );
  }, []);

  const offerUndo = useCallback((routine: Routine, completionId: string | null) => {
    if (undoTimer.current !== null) window.clearTimeout(undoTimer.current);
    setRecent({
      routineId: routine.routineId,
      routineName: routine.name,
      completionId,
      until: Date.now() + UNDO_WINDOW_MS,
    });
    undoTimer.current = window.setTimeout(() => {
      undoTimer.current = null;
      setRecent(null);
    }, UNDO_WINDOW_MS);
  }, []);

  useEffect(
    () => () => {
      if (undoTimer.current !== null) window.clearTimeout(undoTimer.current);
    },
    [],
  );

  const complete = useCallback(
    async (routineId: string): Promise<boolean> => {
      if (busyId !== null) return false;
      const current = routines.find((r) => r.routineId === routineId);
      if (current === undefined) return false;
      setBusyId(routineId);
      setError(null);
      // Optimistic: the row reads as done the instant it is tapped. Only the
      // status flips - no due date is invented; the server's reply brings it.
      replace({ ...current, status: current.paused ? "paused" : "ok", lastDoneOn: todayIsoDate() });
      try {
        const updated = await completeRoutine(routineId);
        replace(updated);
        // `POST /complete` returns the routine, not the completion. To offer
        // Undo we need the completion's id, so we read the list back and take
        // the newest (ordered by doneOn, then id - the last entry). A failure
        // here just means no Undo button; the completion itself stood.
        let completionId: string | null = null;
        try {
          const completions = await listCompletions(routineId);
          completionId = completions[completions.length - 1]?.completionId ?? null;
        } catch {
          completionId = null;
        }
        offerUndo(updated, completionId);
        afterWrite();
        return true;
      } catch (err) {
        replace(current); // Roll the optimistic flip back.
        setError(describe(err, `Couldn’t mark “${current.name}” done`));
        return false;
      } finally {
        setBusyId(null);
      }
    },
    [busyId, routines, replace, offerUndo, afterWrite],
  );

  const undo = useCallback(async (): Promise<boolean> => {
    if (recent === null || recent.completionId === null || busyId !== null) return false;
    setBusyId(recent.routineId);
    setError(null);
    try {
      await undoCompletion(recent.routineId, recent.completionId);
      setRecent(null);
      if (undoTimer.current !== null) window.clearTimeout(undoTimer.current);
      undoTimer.current = null;
      afterWrite();
      return true;
    } catch (err) {
      setError(describe(err, `Couldn’t undo “${recent.routineName}”`));
      return false;
    } finally {
      setBusyId(null);
    }
  }, [recent, busyId, afterWrite]);

  const create = useCallback(
    async (body: CreateRoutineBody): Promise<Routine | null> => {
      setError(null);
      try {
        const created = await createRoutine(body);
        setRoutines((prev) => orderRoutines([...prev, created]));
        afterWrite();
        return created;
      } catch (err) {
        setError(describe(err, `Couldn’t add “${body.name}”`));
        return null;
      }
    },
    [afterWrite],
  );

  const patch = useCallback(
    async (routineId: string, body: RoutinePatch): Promise<Routine | null> => {
      if (busyId !== null) return null;
      setBusyId(routineId);
      setError(null);
      try {
        const updated = await patchRoutine(routineId, body);
        replace(updated);
        afterWrite();
        return updated;
      } catch (err) {
        setError(describe(err, "Couldn’t update the routine"));
        return null;
      } finally {
        setBusyId(null);
      }
    },
    [busyId, replace, afterWrite],
  );

  const remove = useCallback(
    async (routineId: string): Promise<boolean> => {
      if (busyId !== null) return false;
      setBusyId(routineId);
      setError(null);
      try {
        await deleteRoutine(routineId);
        setRoutines((prev) => prev.filter((r) => r.routineId !== routineId));
        afterWrite();
        return true;
      } catch (err) {
        setError(describe(err, "Couldn’t remove the routine"));
        return false;
      } finally {
        setBusyId(null);
      }
    },
    [busyId, afterWrite],
  );

  const estimate = useCallback(
    (name: string, context?: string) => estimateRoutine(name, context),
    [],
  );

  return {
    routines,
    status,
    error,
    busyId,
    recent,
    reload,
    complete,
    undo,
    create,
    patch,
    remove,
    estimate,
    clearError,
  };
}
