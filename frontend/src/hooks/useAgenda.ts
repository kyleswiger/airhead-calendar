import { useCallback, useEffect, useRef, useState } from "react";

import { fetchAgenda } from "../api";
import type { AgendaResponse } from "../types";
import { parseAgenda } from "../lib/parse";

const CACHE_KEY = "airhead.agenda.v1";
const REFRESH_MS = 5 * 60_000;

export type AgendaStatus = "loading" | "ready" | "error";

export interface AgendaState {
  data: AgendaResponse | null;
  status: AgendaStatus;
  /** Set only when the *latest* attempt failed; `data` may still be good. */
  error: string | null;
  /** Epoch ms of the last successful load, including a load from cache. */
  fetchedAt: number | null;
  /** Last attempt failed but we are still showing something. */
  stale: boolean;
  reload: () => void;
}

interface CacheEnvelope {
  fetchedAt: number;
  payload: unknown;
}

/**
 * A kitchen screen that goes blank on a fetch failure is worse than useless,
 * so the last good payload is persisted and re-shown with a staleness marker
 * instead of an error page. Only a cold start with no cache at all shows an
 * error state.
 */
function readCache(): { data: AgendaResponse; fetchedAt: number } | null {
  try {
    const raw = window.localStorage.getItem(CACHE_KEY);
    if (raw === null) return null;
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null) return null;
    const envelope = parsed as Partial<CacheEnvelope>;
    if (typeof envelope.fetchedAt !== "number") return null;
    return { data: parseAgenda(envelope.payload), fetchedAt: envelope.fetchedAt };
  } catch {
    return null;
  }
}

function writeCache(data: AgendaResponse, fetchedAt: number): void {
  try {
    const envelope: CacheEnvelope = { fetchedAt, payload: data };
    window.localStorage.setItem(CACHE_KEY, JSON.stringify(envelope));
  } catch {
    // A full or disabled localStorage must never take the display down.
  }
}

export function useAgenda(start: string, end: string): AgendaState {
  const [data, setData] = useState<AgendaResponse | null>(null);
  const [status, setStatus] = useState<AgendaStatus>("loading");
  const [error, setError] = useState<string | null>(null);
  const [fetchedAt, setFetchedAt] = useState<number | null>(null);
  const [nonce, setNonce] = useState(0);
  const hasData = useRef(false);

  // Paint cached data before the first request resolves.
  useEffect(() => {
    const cached = readCache();
    if (cached === null || hasData.current) return;
    hasData.current = true;
    setData(cached.data);
    setFetchedAt(cached.fetchedAt);
    setStatus("ready");
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;

    const run = async () => {
      try {
        const next = await fetchAgenda({ start, end }, controller.signal);
        if (cancelled) return;
        const now = Date.now();
        hasData.current = true;
        setData(next);
        setFetchedAt(now);
        setError(null);
        setStatus("ready");
        writeCache(next, now);
      } catch (err) {
        if (cancelled || controller.signal.aborted) return;
        const message = err instanceof Error ? err.message : "Could not reach the calendar";
        setError(message);
        // Keep whatever is on screen; only a data-less failure is an error page.
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
  }, [start, end, nonce]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);

  return { data, status, error, fetchedAt, stale: error !== null && data !== null, reload };
}
