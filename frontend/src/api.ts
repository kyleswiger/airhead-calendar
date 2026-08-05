/**
 * The only module that talks to the network. Everything above it sees plain
 * typed data and never a `Response`.
 *
 * Two modes:
 *  - `VITE_API_BASE` set   -> real `GET /api/agenda`, with the M1 header shim
 *    `X-Airhead-Member` (a placeholder for the Cognito authorizer; the server
 *    still derives visibility itself - the display never asks for a tier or a
 *    visibility level, per the contract).
 *  - `VITE_API_BASE` unset -> the bundled fixture, rebased onto today so the
 *    site renders a believable week before the API Lambda exists.
 *
 * Alex / Sam / Riley in the fixture are placeholders. This repo is public.
 */

import agendaFixture from "./fixtures/agenda.sample.json";
import eventsFixture from "./fixtures/events.sample.json";
import { rebaseAgenda, shiftRow } from "./lib/agenda";
import { daysBetween, todayIsoDate } from "./lib/format";
import { isRecord, parseAgenda, parseAgentTurn, parseApiError, parseRow } from "./lib/parse";
import type { AgendaResponse, AgentTurnRequest, AgentTurnResponse, EventRow } from "./types";
import { isEventRow } from "./types";

// Narrowed off Vite's `any`-typed index signature so nothing downstream is `any`.
const env: Record<string, string | undefined> = import.meta.env;

const API_BASE = env["VITE_API_BASE"];
/** M1 header shim actor. Defaults to the adult admin placeholder. */
const MEMBER_ID = env["VITE_MEMBER_ID"] ?? "mem_alex";

/**
 * Who this screen is acting as. Exported because the agent panel has to *show*
 * it: the kitchen display is in a shared space, and the acting identity decides
 * what the agent is allowed to do (PRD §10.1 - `actor_member_id` is injected
 * server-side from this header and the model cannot spoof it).
 */
export const actingMemberId = MEMBER_ID;

/** True when we are rendering the bundled sample rather than live data. */
export const usingFixture = typeof API_BASE !== "string" || API_BASE.length === 0;

export class ApiError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(message: string, code: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
  }
}

function baseUrl(): string {
  return typeof API_BASE === "string" ? API_BASE.replace(/\/+$/, "") : "";
}

function headers(): Record<string, string> {
  return { Accept: "application/json", "X-Airhead-Member": MEMBER_ID };
}

async function readJson(res: Response): Promise<unknown> {
  const text = await res.text();
  if (text.length === 0) return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    throw new ApiError("Response was not JSON", "bad_response", res.status);
  }
}

/** M1 error envelope in, `ApiError` out. Every call funnels through here. */
async function unwrap(res: Response): Promise<unknown> {
  const body = await readJson(res);
  if (!res.ok) {
    const parsed = parseApiError(body);
    throw new ApiError(
      parsed?.message ?? `Request failed (${res.status})`,
      parsed?.code ?? "http_error",
      res.status,
    );
  }
  return body;
}

async function request(path: string, signal?: AbortSignal): Promise<unknown> {
  const init: RequestInit = { headers: headers() };
  if (signal !== undefined) init.signal = signal;
  return unwrap(await fetch(`${baseUrl()}${path}`, init));
}

async function postJson(path: string, payload: unknown, signal?: AbortSignal): Promise<unknown> {
  const init: RequestInit = {
    method: "POST",
    headers: { ...headers(), "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  };
  if (signal !== undefined) init.signal = signal;
  return unwrap(await fetch(`${baseUrl()}${path}`, init));
}

// --- fixture mode ----------------------------------------------------------

const FIXTURE_AGENDA: AgendaResponse = parseAgenda(agendaFixture as unknown);

/** Days the whole fixture is shifted by so day one lands on today. */
const FIXTURE_DELTA = daysBetween(FIXTURE_AGENDA.range.start, todayIsoDate());

function fixtureAgenda(): AgendaResponse {
  return rebaseAgenda(FIXTURE_AGENDA, todayIsoDate());
}

function fixtureEvents(): ReadonlyMap<string, EventRow> {
  const raw = eventsFixture as unknown;
  const bag = isRecord(raw) ? raw["events"] : undefined;
  const out = new Map<string, EventRow>();
  if (!isRecord(bag)) return out;
  for (const [id, value] of Object.entries(bag)) {
    const row = parseRow(value);
    if (row !== null && isEventRow(row)) {
      const shifted = shiftRow(row, FIXTURE_DELTA);
      if (isEventRow(shifted)) out.set(id, shifted);
    }
  }
  return out;
}

const FIXTURE_EVENTS = fixtureEvents();

// --- public surface --------------------------------------------------------

export interface AgendaQuery {
  /** YYYY-MM-DD inclusive. */
  start: string;
  /** YYYY-MM-DD inclusive, max 31 days after start. */
  end: string;
}

/**
 * The display always asks for the full tier range. Suppression is the *view's*
 * job, not the query's: asking the server to omit T3 would be exactly the
 * "hide something that mattered" failure the tier system exists to prevent.
 */
export async function fetchAgenda(query: AgendaQuery, signal?: AbortSignal): Promise<AgendaResponse> {
  if (usingFixture) return fixtureAgenda();
  const params = new URLSearchParams({ start: query.start, end: query.end });
  return parseAgenda(await request(`/api/agenda?${params.toString()}`, signal));
}

/**
 * Details behind a collapsed busy band. The agenda row carries only `eventIds`
 * (the contract keeps the band cheap), so expansion resolves them one by one.
 * A failure here degrades to "count only" - the band never disappears.
 */
export async function fetchEvents(
  eventIds: readonly string[],
  signal?: AbortSignal,
): Promise<EventRow[]> {
  if (usingFixture) {
    return eventIds
      .map((id) => FIXTURE_EVENTS.get(id))
      .filter((row): row is EventRow => row !== undefined);
  }
  const settled = await Promise.allSettled(
    eventIds.map((id) => request(`/api/events/${encodeURIComponent(id)}`, signal)),
  );
  const rows: EventRow[] = [];
  for (const result of settled) {
    if (result.status !== "fulfilled") continue;
    const row = parseRow(result.value);
    if (row !== null && isEventRow(row)) rows.push(row);
  }
  return rows;
}

/**
 * One conversational turn (`docs/M2-CONTRACT.md`).
 *
 * There is no fixture branch here on purpose. The agenda can be faked because a
 * fake agenda is obviously sample data; a faked *agent reply* would claim a
 * write happened that never did. With no API configured this fails, and the
 * panel says so.
 */
export async function postAgentTurn(
  body: AgentTurnRequest,
  signal?: AbortSignal,
): Promise<AgentTurnResponse> {
  return parseAgentTurn(await postJson("/api/agent/turn", body, signal));
}
