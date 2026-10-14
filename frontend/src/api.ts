/**
 * The only module that talks to the network. Everything above it sees plain
 * typed data and never a `Response`.
 *
 * Two modes:
 *  - `VITE_API_BASE` set   -> the real API, in one of two realms chosen on the
 *    launch screen: `demo` (`/api/*`, the public placeholder household) or
 *    `family` (`/family/api/*`, Cognito access token required - API Gateway
 *    verifies it). Either way `X-Airhead-Member` names who is acting, and the
 *    server resolves it against that realm's roster only and derives
 *    visibility itself - the display never asks for a tier or a visibility
 *    level, per the contract.
 *  - `VITE_API_BASE` unset -> the bundled fixture, rebased onto today so the
 *    site renders a believable week before the API Lambda exists.
 *
 * Alex / Sam / Riley in the fixture are placeholders. This repo is public.
 */

import { accessToken, AuthError, invalidateSession } from "./auth";
import agendaFixture from "./fixtures/agenda.sample.json";
import eventsFixture from "./fixtures/events.sample.json";
import { rebaseAgenda, shiftRow } from "./lib/agenda";
import { daysBetween, todayIsoDate } from "./lib/format";
import {
  fixtureCompleteRoutine,
  fixtureCreateRoutine,
  fixtureDeleteRoutine,
  fixtureDueEvents,
  fixtureEstimate,
  fixtureListCompletions,
  fixtureListRoutines,
  fixturePatchRoutine,
  fixtureUndoCompletion,
} from "./lib/fixtureRoutines";
import {
  isRecord,
  parseAgenda,
  parseAgentTurn,
  parseApiError,
  parseCompletions,
  parseEstimate,
  parseMember,
  parseRoutineEnvelope,
  parseRoutines,
  parseRow,
} from "./lib/parse";
import type {
  AgendaResponse,
  AgentTurnRequest,
  AgentTurnResponse,
  Completion,
  CompleteRoutineBody,
  CreateRoutineBody,
  EstimateResponse,
  EventRow,
  Member,
  Routine,
  RoutinePatch,
} from "./types";
import { isEventRow } from "./types";

// Narrowed off Vite's `any`-typed index signature so nothing downstream is `any`.
const env: Record<string, string | undefined> = import.meta.env;

const API_BASE = env["VITE_API_BASE"];
/** Demo-realm actor. Defaults to the adult admin placeholder. */
export const DEMO_MEMBER_ID = env["VITE_MEMBER_ID"] ?? "mem_alex";

/** True when we are rendering the bundled sample rather than live data. */
export const usingFixture = typeof API_BASE !== "string" || API_BASE.length === 0;

export type Realm = "demo" | "family";

let realm: Realm = "demo";
let actingMember = DEMO_MEMBER_ID;

/** Set once per screen, before anything fetches (`Root` remounts the app on change). */
export function setRealm(next: Realm): void {
  realm = next;
}

/**
 * Who this screen is acting as. The kitchen display is in a shared space, and
 * the acting identity decides what the agent and the API allow (PRD §10.1 -
 * `actor_member_id` is injected server-side from this header and the model
 * cannot spoof it).
 */
export function setActingMember(memberId: string): void {
  actingMember = memberId;
}

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

function realmUrl(path: string): string {
  return `${baseUrl()}${realm === "family" ? "/family" : ""}${path}`;
}

async function headers(forceRefresh = false): Promise<Record<string, string>> {
  const out: Record<string, string> = { Accept: "application/json", "X-Airhead-Member": actingMember };
  if (realm === "family") out["Authorization"] = `Bearer ${await accessToken(forceRefresh)}`;
  return out;
}

/** Thrown when the family session is gone; `Root` has already returned to the launch screen. */
export class SignedOutError extends ApiError {
  constructor() {
    super("Signed out", "signed_out", 401);
  }
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

/**
 * A 401 from API Gateway's JWT authorizer has no M1 envelope (`{"message":
 * "Unauthorized"}`); a 401 from the app does (`unknown member`). Only the
 * former is about the token.
 */
async function isGatewayReject(res: Response): Promise<boolean> {
  if (res.status !== 401) return false;
  try {
    return parseApiError(await res.clone().json()) === null;
  } catch {
    return true;
  }
}

async function send(
  method: "GET" | "POST" | "PATCH" | "DELETE",
  path: string,
  payload?: unknown,
  signal?: AbortSignal,
): Promise<unknown> {
  const attempt = async (forceRefresh: boolean): Promise<Response> => {
    let hdrs: Record<string, string>;
    try {
      hdrs = await headers(forceRefresh);
    } catch (err) {
      if (err instanceof AuthError && (err.code === "NotSignedIn" || err.code === "NotAuthorizedException")) {
        throw new SignedOutError();
      }
      throw err; // Network trouble reaching Cognito: keep the session, show stale data.
    }
    const init: RequestInit = { method, headers: hdrs };
    if (payload !== undefined) {
      init.headers = { ...hdrs, "Content-Type": "application/json" };
      init.body = JSON.stringify(payload);
    }
    if (signal !== undefined) init.signal = signal;
    return fetch(realmUrl(path), init);
  };

  let res = await attempt(false);
  if (realm === "family" && (await isGatewayReject(res))) {
    // Clock skew or a token revoked mid-hour: one forced refresh, then give up.
    res = await attempt(true);
    if (await isGatewayReject(res)) {
      invalidateSession();
      throw new SignedOutError();
    }
  }
  return unwrap(res);
}

async function request(path: string, signal?: AbortSignal): Promise<unknown> {
  return send("GET", path, undefined, signal);
}

async function sendJson(
  method: "POST" | "PATCH",
  path: string,
  payload: unknown,
  signal?: AbortSignal,
): Promise<unknown> {
  return send(method, path, payload, signal);
}

async function postJson(path: string, payload: unknown, signal?: AbortSignal): Promise<unknown> {
  return sendJson("POST", path, payload, signal);
}

async function deleteRequest(path: string, signal?: AbortSignal): Promise<void> {
  await send("DELETE", path, undefined, signal);
}

// --- sign-in surface ---------------------------------------------------------

export interface AuthConfig {
  familyLogin: boolean;
  region: string | null;
  clientId: string | null;
}

/** Public, realm-less: the launch screen asks before anyone has signed in. */
export async function fetchAuthConfig(signal?: AbortSignal): Promise<AuthConfig> {
  const init: RequestInit = { headers: { Accept: "application/json" } };
  if (signal !== undefined) init.signal = signal;
  const body = await unwrap(await fetch(`${baseUrl()}/api/auth/config`, init));
  if (!isRecord(body)) throw new ApiError("Bad auth config", "bad_response", 200);
  const region = body["region"];
  const clientId = body["clientId"];
  return {
    familyLogin: body["familyLogin"] === true,
    region: typeof region === "string" ? region : null,
    clientId: typeof clientId === "string" ? clientId : null,
  };
}

/** The current realm's roster; needs no actor, so the screen can pick one from it. */
export async function fetchMembers(signal?: AbortSignal): Promise<Member[]> {
  const body = await request("/api/members", signal);
  const raw = isRecord(body) ? body["members"] : undefined;
  return Array.isArray(raw) ? raw.map(parseMember).filter((m): m is Member => m !== null) : [];
}

// --- fixture mode ----------------------------------------------------------

const FIXTURE_AGENDA: AgendaResponse = parseAgenda(agendaFixture as unknown);

/** Days the whole fixture is shifted by so day one lands on today. */
const FIXTURE_DELTA = daysBetween(FIXTURE_AGENDA.range.start, todayIsoDate());

/**
 * The rebased sample plus the routines' due events, exactly as the real
 * `GET /api/agenda` would carry them after `reproject`. Days the sample lacks
 * are created so an overdue row rolled forward to today always shows.
 */
function fixtureAgenda(): AgendaResponse {
  const today = todayIsoDate();
  const agenda = rebaseAgenda(FIXTURE_AGENDA, today);
  const days = agenda.days.map((day) => ({ ...day, rows: [...day.rows] }));
  for (const due of fixtureDueEvents(today)) {
    let day = days.find((d) => d.date === due.startLocal);
    if (day === undefined) {
      day = { date: due.startLocal, rows: [] };
      days.push(day);
    }
    day.rows.push(due);
  }
  days.sort((a, b) => a.date.localeCompare(b.date));
  return { ...agenda, days };
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

/* ------------------------------------------------------------ routines -- */

/**
 * `docs/ROUTINES-CONTRACT.md` § HTTP. Every date on the wire is a bare
 * household-local `YYYY-MM-DD`; the display never derives one. In fixture mode
 * `lib/fixtureRoutines.ts` plays the server so the demo is interactive.
 */

const ROUTINES = "/api/routines";

function routinePath(id: string, suffix = ""): string {
  return `${ROUTINES}/${encodeURIComponent(id)}${suffix}`;
}

export async function listRoutines(signal?: AbortSignal): Promise<Routine[]> {
  if (usingFixture) return fixtureListRoutines();
  return parseRoutines(await request(ROUTINES, signal));
}

export async function createRoutine(body: CreateRoutineBody, signal?: AbortSignal): Promise<Routine> {
  if (usingFixture) return fixtureCreateRoutine(body);
  return parseRoutineEnvelope(await postJson(ROUTINES, body, signal));
}

/** Returns the updated *routine*, not the completion (contract). */
export async function completeRoutine(
  id: string,
  body: CompleteRoutineBody = {},
  signal?: AbortSignal,
): Promise<Routine> {
  if (usingFixture) {
    const routine = fixtureCompleteRoutine(id, body.doneOn, body.note, actingMember);
    if (routine === null) throw new ApiError("Routine not found", "not_found", 404);
    return routine;
  }
  return parseRoutineEnvelope(await postJson(routinePath(id, "/complete"), body, signal));
}

/** Ordered by doneOn, then id - so the last entry is the newest. */
export async function listCompletions(id: string, signal?: AbortSignal): Promise<Completion[]> {
  if (usingFixture) return fixtureListCompletions(id);
  return parseCompletions(await request(routinePath(id, "/completions"), signal));
}

export async function undoCompletion(id: string, completionId: string, signal?: AbortSignal): Promise<void> {
  if (usingFixture) {
    if (!fixtureUndoCompletion(id, completionId)) {
      throw new ApiError("Completion not found", "not_found", 404);
    }
    return;
  }
  await deleteRequest(routinePath(id, `/completions/${encodeURIComponent(completionId)}`), signal);
}

export async function patchRoutine(id: string, patch: RoutinePatch, signal?: AbortSignal): Promise<Routine> {
  if (usingFixture) {
    const routine = fixturePatchRoutine(id, patch);
    if (routine === null) throw new ApiError("Routine not found", "not_found", 404);
    return routine;
  }
  return parseRoutineEnvelope(await sendJson("PATCH", routinePath(id), patch, signal));
}

export async function deleteRoutine(id: string, signal?: AbortSignal): Promise<void> {
  if (usingFixture) {
    fixtureDeleteRoutine(id);
    return;
  }
  await deleteRequest(routinePath(id), signal);
}

/** Writes nothing. A catalog hit costs no model call. */
export async function estimateRoutine(
  name: string,
  context?: string,
  signal?: AbortSignal,
): Promise<EstimateResponse> {
  if (usingFixture) return fixtureEstimate(name, context);
  const body: { name: string; context?: string } = { name };
  if (context !== undefined && context.length > 0) body.context = context;
  return parseEstimate(await postJson(`${ROUTINES}/estimate`, body, signal));
}
