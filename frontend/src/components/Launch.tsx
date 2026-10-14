import { useEffect, useRef, useState, type FormEvent } from "react";

import { fetchAuthConfig, type AuthConfig } from "../api";
import { AuthError, completeNewPassword, signIn, type CognitoConfig } from "../auth";

interface LaunchProps {
  /** Shown above the buttons, e.g. after a session was revoked. */
  notice?: string;
  onFamily: () => void;
  onDemo: () => void;
}

type Step =
  | { kind: "choose" }
  | { kind: "signIn" }
  | { kind: "newPassword"; username: string; session: string };

function describe(err: unknown): string {
  if (err instanceof AuthError) {
    switch (err.code) {
      case "NotAuthorizedException":
      case "UserNotFoundException":
        return "That username and password didn’t match.";
      case "InvalidPasswordException":
        return err.message;
      case "PasswordResetRequiredException":
        return "This account needs a password reset by the owner.";
      case "TooManyRequestsException":
      case "LimitExceededException":
        return "Too many attempts. Wait a minute and try again.";
      default:
        return err.message;
    }
  }
  return "Couldn’t reach the sign-in service. Check the connection and try again.";
}

/**
 * First screen on a fresh device: the private family calendar (Cognito
 * sign-in) or the public demo. Once signed in, the kitchen screen skips this
 * on every reload until someone signs out or the session is revoked.
 */
export function Launch({ notice, onFamily, onDemo }: LaunchProps) {
  const [config, setConfig] = useState<AuthConfig | null>(null);
  const [configError, setConfigError] = useState(false);
  const [step, setStep] = useState<Step>({ kind: "choose" });
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const firstField = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    fetchAuthConfig(controller.signal)
      .then(setConfig)
      .catch(() => {
        if (!controller.signal.aborted) setConfigError(true);
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (step.kind !== "choose") firstField.current?.focus();
  }, [step.kind]);

  const cognito: CognitoConfig | null =
    config?.familyLogin && config.region !== null && config.clientId !== null
      ? { region: config.region, clientId: config.clientId }
      : null;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (cognito === null || busy) return;
    setBusy(true);
    setError(null);
    try {
      if (step.kind === "newPassword") {
        await completeNewPassword(cognito, step.username, step.session, newPassword);
        onFamily();
        return;
      }
      const result = await signIn(cognito, username.trim(), password);
      if (result.kind === "newPasswordRequired") {
        setPassword("");
        setStep({ kind: "newPassword", username: result.username, session: result.session });
      } else {
        onFamily();
      }
    } catch (err) {
      setError(describe(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="launch">
      <div className="launch__card">
        <h1 className="launch__title">Airhead</h1>
        <p className="launch__subtitle">Family calendar</p>
        {notice !== undefined ? (
          <p className="launch__notice" role="status">
            {notice}
          </p>
        ) : null}

        {step.kind === "choose" ? (
          <div className="launch__choices">
            <button
              type="button"
              className="launch__btn launch__btn--primary"
              disabled={cognito === null}
              onClick={() => setStep({ kind: "signIn" })}
            >
              Sign in to our calendar
            </button>
            {config !== null && cognito === null ? (
              <p className="launch__hint">Family sign-in isn’t set up on this server yet.</p>
            ) : null}
            {configError ? <p className="launch__hint">Can’t reach the server right now.</p> : null}
            <button type="button" className="launch__btn" onClick={onDemo}>
              View the demo
            </button>
          </div>
        ) : (
          <form className="launch__form" onSubmit={(e) => void submit(e)}>
            {step.kind === "signIn" ? (
              <>
                <label className="launch__label">
                  Username
                  <input
                    ref={firstField}
                    className="launch__input"
                    autoComplete="username"
                    autoCapitalize="none"
                    spellCheck={false}
                    value={username}
                    onChange={(e) => setUsername(e.target.value)}
                    required
                  />
                </label>
                <label className="launch__label">
                  Password
                  <input
                    className="launch__input"
                    type="password"
                    autoComplete="current-password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    required
                  />
                </label>
              </>
            ) : (
              <label className="launch__label">
                Choose a new password (12+ characters, with a number)
                <input
                  ref={firstField}
                  className="launch__input"
                  type="password"
                  autoComplete="new-password"
                  minLength={12}
                  value={newPassword}
                  onChange={(e) => setNewPassword(e.target.value)}
                  required
                />
              </label>
            )}
            {error !== null ? (
              <p className="launch__error" role="alert">
                {error}
              </p>
            ) : null}
            <div className="launch__actions">
              <button type="submit" className="launch__btn launch__btn--primary" disabled={busy}>
                {busy ? "Signing in…" : step.kind === "newPassword" ? "Set password" : "Sign in"}
              </button>
              <button
                type="button"
                className="launch__btn"
                disabled={busy}
                onClick={() => {
                  setError(null);
                  setStep({ kind: "choose" });
                }}
              >
                Back
              </button>
            </div>
          </form>
        )}
      </div>
    </main>
  );
}
