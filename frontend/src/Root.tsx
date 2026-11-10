import { useCallback, useEffect, useState } from "react";

import {
  DEMO_MEMBER_ID,
  fetchMembers,
  setActingMember,
  setRealm,
  SignedOutError,
  usingFixture,
  type Realm,
} from "./api";
import { App } from "./App";
import { hasSession, onSessionLost, signOut } from "./auth";
import { Launch } from "./components/Launch";
import { clearAgendaCache } from "./hooks/useAgenda";

const ACTOR_KEY = "airhead.member.family";

type Screen =
  | { kind: "launch"; notice?: string }
  | { kind: "resolving" }
  | { kind: "error"; message: string }
  | { kind: "app"; realm: Realm; actorId: string };

function readActor(): string | null {
  try {
    return window.localStorage.getItem(ACTOR_KEY);
  } catch {
    return null;
  }
}

function storeActor(memberId: string): void {
  try {
    window.localStorage.setItem(ACTOR_KEY, memberId);
  } catch {
    // Falls back to the first adult on the next load.
  }
}

/**
 * Who the family screen acts as: the last choice on this device if it is still
 * on the roster, else the first adult. Offline at boot, the last choice is
 * trusted so the cached agenda can still paint.
 */
async function resolveFamilyActor(): Promise<string> {
  const stored = readActor();
  try {
    const roster = await fetchMembers();
    if (stored !== null && roster.some((m) => m.memberId === stored)) return stored;
    const pick = roster.find((m) => m.role === "adult") ?? roster[0];
    if (pick === undefined) throw new Error("No family members are set up yet.");
    storeActor(pick.memberId);
    return pick.memberId;
  } catch (err) {
    if (err instanceof SignedOutError || stored === null) throw err;
    return stored;
  }
}

function initialScreen(): Screen {
  if (usingFixture) return { kind: "app", realm: "demo", actorId: DEMO_MEMBER_ID };
  return hasSession() ? { kind: "resolving" } : { kind: "launch" };
}

export function Root() {
  const [screen, setScreen] = useState<Screen>(initialScreen);

  const enterFamily = useCallback(() => {
    setRealm("family");
    setScreen({ kind: "resolving" });
  }, []);

  const enterDemo = useCallback(() => {
    setRealm("demo");
    setActingMember(DEMO_MEMBER_ID);
    setScreen({ kind: "app", realm: "demo", actorId: DEMO_MEMBER_ID });
  }, []);

  // A revoked or expired session sends the screen back to sign-in, and takes
  // the family agenda off the device with it.
  useEffect(
    () =>
      onSessionLost(() => {
        clearAgendaCache("family");
        setScreen({ kind: "launch", notice: "You’ve been signed out. Please sign in again." });
      }),
    [],
  );

  useEffect(() => {
    if (screen.kind !== "resolving") return;
    let cancelled = false;
    setRealm("family");
    setActingMember(readActor() ?? "");
    resolveFamilyActor()
      .then((actorId) => {
        if (cancelled) return;
        setActingMember(actorId);
        setScreen({ kind: "app", realm: "family", actorId });
      })
      .catch((err: unknown) => {
        if (cancelled || err instanceof SignedOutError) return; // onSessionLost handles it.
        const message = err instanceof Error ? err.message : "Couldn’t load the family.";
        setScreen({ kind: "error", message });
      });
    return () => {
      cancelled = true;
    };
  }, [screen.kind]);

  const changeActor = useCallback((actorId: string) => {
    setActingMember(actorId);
    setScreen((s) => {
      if (s.kind !== "app") return s;
      if (s.realm === "family") storeActor(actorId);
      return { ...s, actorId };
    });
  }, []);

  const exit = useCallback(async (realm: Realm) => {
    if (realm === "family") {
      await signOut();
      clearAgendaCache("family");
    }
    setRealm("demo");
    setScreen({ kind: "launch" });
  }, []);

  switch (screen.kind) {
    case "launch":
      return (
        <Launch
          {...(screen.notice !== undefined ? { notice: screen.notice } : {})}
          onFamily={enterFamily}
          onDemo={enterDemo}
        />
      );
    case "resolving":
      return (
        <main className="launch">
          <p className="content__note">Opening the family calendar…</p>
        </main>
      );
    case "error":
      return (
        <main className="launch">
          <div className="content__error">
            <p className="content__note">{screen.message}</p>
            <button type="button" className="nav__btn" onClick={() => setScreen({ kind: "resolving" })}>
              Try again
            </button>
            <button type="button" className="nav__btn" onClick={() => void exit("family")}>
              Sign out
            </button>
          </div>
        </main>
      );
    case "app":
      return (
        // Keyed so a realm or actor change starts clean: agent conversations are
        // bound to the member who started them, and caches to the realm.
        <App
          key={`${screen.realm}:${screen.actorId}`}
          realm={screen.realm}
          actorId={screen.actorId}
          onActorChange={changeActor}
          {...(usingFixture ? {} : { onExit: () => void exit(screen.realm) })}
        />
      );
  }
}
