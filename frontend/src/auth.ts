/**
 * Family sign-in against Cognito, with no SDK: three JSON calls to the
 * cognito-idp endpoint (InitiateAuth, RespondToAuthChallenge, RevokeToken).
 *
 * The kitchen screen is a shared device that signs in once and must then stay
 * signed in for months (PRD §13), so the rules here are about *not* signing
 * out:
 *  - the refresh token is persisted, and access tokens are refreshed shortly
 *    before they expire;
 *  - only a definitive rejection from Cognito (revoked or expired refresh
 *    token) ends the session. A network failure while the Wi-Fi blips keeps
 *    the session and lets the agenda's stale-cache path carry the screen.
 */

const STORE_KEY = "airhead.session.v1";
/** Refresh this long before the access token's real expiry. */
const REFRESH_SKEW_MS = 2 * 60_000;

interface Session {
  username: string;
  region: string;
  clientId: string;
  refreshToken: string;
  accessToken: string;
  /** Epoch ms. */
  expiresAt: number;
}

export interface CognitoConfig {
  region: string;
  clientId: string;
}

/** Cognito said no. `code` is its `__type`, e.g. NotAuthorizedException. */
export class AuthError extends Error {
  readonly code: string;

  constructor(message: string, code: string) {
    super(message);
    this.name = "AuthError";
    this.code = code;
  }
}

export type SignInResult =
  | { kind: "signedIn" }
  | { kind: "newPasswordRequired"; username: string; session: string };

// --- storage ----------------------------------------------------------------

function load(): Session | null {
  try {
    const raw = window.localStorage.getItem(STORE_KEY);
    if (raw === null) return null;
    const parsed = JSON.parse(raw) as Partial<Session>;
    if (
      typeof parsed.username !== "string" ||
      typeof parsed.region !== "string" ||
      typeof parsed.clientId !== "string" ||
      typeof parsed.refreshToken !== "string" ||
      typeof parsed.accessToken !== "string" ||
      typeof parsed.expiresAt !== "number"
    ) {
      return null;
    }
    return parsed as Session;
  } catch {
    return null;
  }
}

function save(session: Session | null): void {
  try {
    if (session === null) window.localStorage.removeItem(STORE_KEY);
    else window.localStorage.setItem(STORE_KEY, JSON.stringify(session));
  } catch {
    // Storage disabled: the session lives for this page load only.
  }
}

let current: Session | null = load();
const listeners = new Set<() => void>();

/** Called once when the session ends without the person asking (revoked/expired). */
export function onSessionLost(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function lose(): void {
  current = null;
  save(null);
  for (const listener of listeners) listener();
}

export function hasSession(): boolean {
  return current !== null;
}

export function signedInAs(): string | null {
  return current?.username ?? null;
}

// --- cognito ----------------------------------------------------------------

async function cognito(
  region: string,
  target: string,
  body: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  const res = await fetch(`https://cognito-idp.${region}.amazonaws.com/`, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-amz-json-1.1",
      "X-Amz-Target": `AWSCognitoIdentityProviderService.${target}`,
    },
    body: JSON.stringify(body),
  });
  let json: Record<string, unknown> = {};
  try {
    json = (await res.json()) as Record<string, unknown>;
  } catch {
    // Empty body (RevokeToken) or a proxy page; status decides.
  }
  if (!res.ok) {
    const type = typeof json["__type"] === "string" ? json["__type"] : "UnknownError";
    const message = typeof json["message"] === "string" ? json["message"] : `Sign-in failed (${res.status})`;
    throw new AuthError(message, type.split("#").pop() ?? type);
  }
  return json;
}

interface AuthResult {
  AccessToken?: unknown;
  RefreshToken?: unknown;
  ExpiresIn?: unknown;
}

function adopt(cfg: CognitoConfig, username: string, result: unknown, refreshToken?: string): void {
  const r = (result ?? {}) as AuthResult;
  const access = typeof r.AccessToken === "string" ? r.AccessToken : null;
  const refresh = typeof r.RefreshToken === "string" ? r.RefreshToken : refreshToken;
  const ttl = typeof r.ExpiresIn === "number" ? r.ExpiresIn : 3600;
  if (access === null || refresh === undefined) {
    throw new AuthError("Cognito returned no tokens", "BadResponse");
  }
  current = {
    username,
    region: cfg.region,
    clientId: cfg.clientId,
    refreshToken: refresh,
    accessToken: access,
    expiresAt: Date.now() + ttl * 1000,
  };
  save(current);
}

export async function signIn(cfg: CognitoConfig, username: string, password: string): Promise<SignInResult> {
  const res = await cognito(cfg.region, "InitiateAuth", {
    AuthFlow: "USER_PASSWORD_AUTH",
    ClientId: cfg.clientId,
    AuthParameters: { USERNAME: username, PASSWORD: password },
  });
  if (res["ChallengeName"] === "NEW_PASSWORD_REQUIRED" && typeof res["Session"] === "string") {
    return { kind: "newPasswordRequired", username, session: res["Session"] };
  }
  if (res["ChallengeName"] !== undefined) {
    throw new AuthError(`Unsupported sign-in step: ${String(res["ChallengeName"])}`, "UnsupportedChallenge");
  }
  adopt(cfg, username, res["AuthenticationResult"]);
  return { kind: "signedIn" };
}

/** First sign-in with an admin-issued temporary password. */
export async function completeNewPassword(
  cfg: CognitoConfig,
  username: string,
  session: string,
  newPassword: string,
): Promise<void> {
  const res = await cognito(cfg.region, "RespondToAuthChallenge", {
    ChallengeName: "NEW_PASSWORD_REQUIRED",
    ClientId: cfg.clientId,
    Session: session,
    ChallengeResponses: { USERNAME: username, NEW_PASSWORD: newPassword },
  });
  adopt(cfg, username, res["AuthenticationResult"]);
}

let refreshing: Promise<string> | null = null;

async function refresh(session: Session): Promise<string> {
  try {
    const res = await cognito(session.region, "InitiateAuth", {
      AuthFlow: "REFRESH_TOKEN_AUTH",
      ClientId: session.clientId,
      AuthParameters: { REFRESH_TOKEN: session.refreshToken },
    });
    // Refresh responses carry no new refresh token unless rotation is on.
    adopt(session, session.username, res["AuthenticationResult"], session.refreshToken);
    return current?.accessToken ?? "";
  } catch (err) {
    if (err instanceof AuthError && err.code === "NotAuthorizedException") lose();
    throw err;
  }
}

/**
 * A valid access token, refreshing first if it is about to expire. Concurrent
 * callers share one refresh. `force` is for a token the gateway just rejected.
 */
export async function accessToken(force = false): Promise<string> {
  const session = current;
  if (session === null) throw new AuthError("Not signed in", "NotSignedIn");
  if (!force && session.expiresAt - Date.now() > REFRESH_SKEW_MS) return session.accessToken;
  refreshing ??= refresh(session).finally(() => {
    refreshing = null;
  });
  return refreshing;
}

/** The gateway rejected a freshly refreshed token: the session is no good. */
export function invalidateSession(): void {
  if (current !== null) lose();
}

/** Deliberate sign-out: revoke the refresh token (best effort) and forget it. */
export async function signOut(): Promise<void> {
  const session = current;
  current = null;
  save(null);
  if (session === null) return;
  try {
    await cognito(session.region, "RevokeToken", {
      Token: session.refreshToken,
      ClientId: session.clientId,
    });
  } catch {
    // Offline or already revoked; the local session is gone either way.
  }
}
