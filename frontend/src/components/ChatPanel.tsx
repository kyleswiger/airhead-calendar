import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";

import type { AgentChat } from "../hooks/useAgentChat";
import { isApplied } from "../lib/agent";
import type { Member } from "../types";
import { ConfirmGate } from "./ConfirmGate";
import { MemberTag } from "./MemberTag";

interface ChatPanelProps {
  chat: AgentChat;
  actor: Member | undefined;
  actorId: string;
  onClose: () => void;
}

const FOCUSABLE = "button:not([disabled]), textarea:not([disabled]), [href]";

/**
 * The chat is a *mode over* the day view, not a replacement for it: the panel
 * takes the right-hand third and the calendar stays lit behind it, so the
 * screen never stops being a calendar. Escape and the ✕ both return.
 */
export function ChatPanel({ chat, actor, actorId, onClose }: ChatPanelProps) {
  const { entries, pending, busy, send, answer } = chat;
  const [draft, setDraft] = useState("");
  const panelRef = useRef<HTMLElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const logRef = useRef<HTMLDivElement>(null);

  // Opening must put the caret somewhere useful - except when a gate is
  // already open, in which case ConfirmGate owns the focus and typing is not
  // the next thing to do anyway.
  // Intentionally mount-only: every later focus move belongs to the gate.
  const openWithGate = useRef(pending !== null);
  useEffect(() => {
    if (!openWithGate.current) inputRef.current?.focus();
  }, []);

  /*
   * Focus must never fall out of the panel. It happens more easily than it
   * looks: Send disables itself the moment a turn starts, and the gate's
   * buttons unmount the moment it is answered - in both cases the browser
   * drops focus on <body> and the keyboard stops working entirely. So after
   * any state change, if focus is no longer inside the panel, it comes back to
   * the composer. (ConfirmGate's own effect runs first - child before parent -
   * so an open gate has already claimed focus and this leaves it alone.)
   */
  useEffect(() => {
    const panel = panelRef.current;
    if (panel === null) return;
    const active = document.activeElement;
    if (active !== null && active !== document.body && panel.contains(active)) return;
    if (pending === null) inputRef.current?.focus();
  }, [busy, pending, entries]);

  // Escape is listened for on the document, not the panel: once focus has been
  // anywhere else for a moment, a handler bound to the panel would never fire.
  useEffect(() => {
    const onEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onEscape);
    return () => document.removeEventListener("keydown", onEscape);
  }, [onClose]);

  // Newest turn stays in view. `auto`, not `smooth`: a wall panel that animates
  // a scroll on every turn is a wall panel that fights prefers-reduced-motion.
  useEffect(() => {
    const log = logRef.current;
    if (log !== null) log.scrollTop = log.scrollHeight;
  }, [entries, busy, pending]);

  const dispatch = () => {
    if (pending !== null) return; // The gate has to be answered first.
    send(draft);
    setDraft("");
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    dispatch();
  };

  const onKeyDown = (event: ReactKeyboardEvent<HTMLElement>) => {
    if (event.key !== "Tab") return;
    // Keep focus inside the panel: on a kiosk there is nothing behind it to
    // tab to, and focus landing on the browser chrome is focus lost for good.
    const panel = panelRef.current;
    if (panel === null) return;
    const items = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE));
    const first = items[0];
    const last = items[items.length - 1];
    if (first === undefined || last === undefined) return;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  const onInputKey = (event: ReactKeyboardEvent<HTMLTextAreaElement>) => {
    // Enter sends; Shift+Enter is a newline. An on-screen keyboard has no
    // comfortable modifier, so the common case is the unmodified key.
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      dispatch();
    }
  };

  return (
    <>
      {/* Dimmed, not blacked out - the agenda behind stays readable. */}
      <div className="chat-scrim" onClick={onClose} aria-hidden="true" />
      <aside
        className="chat"
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label="Ask the calendar"
        onKeyDown={onKeyDown}
      >
        <header className="chat__head">
          <div className="chat__ident">
            <h2 className="chat__title">Ask the calendar</h2>
            {/* The screen is shared. Who is asking decides what the agent may
                do, so it is stated on the panel and again on every gate. */}
            <p className="chat__actor">
              <span className="chat__actor-label">Acting as</span>
              <MemberTag member={actor} fallbackId={actorId} />
            </p>
          </div>
          <button type="button" className="chat__close" onClick={onClose} aria-label="Close chat">
            <span aria-hidden="true">✕</span>
          </button>
        </header>

        <div className="chat__log" ref={logRef} role="log" aria-label="Conversation" aria-live="polite">
          {entries.length === 0 ? (
            <p className="chat__hint">
              Try “add soccer practice thursday at 4”, “what does Riley have tomorrow?”, or “move
              dinner to 7”.
            </p>
          ) : (
            entries.map((entry) =>
              entry.role === "agent" ? (
                <div key={entry.id} className="bubble bubble--agent">
                  <p className="bubble__text">{entry.text}</p>
                  {entry.actions.some(isApplied) ? (
                    <p className="bubble__applied">✓ Calendar updated</p>
                  ) : null}
                </div>
              ) : entry.role === "error" ? (
                <div key={entry.id} className="bubble bubble--error">
                  <p className="bubble__label">Couldn’t finish that</p>
                  <p className="bubble__text">{entry.text}</p>
                </div>
              ) : (
                <div key={entry.id} className="bubble bubble--you">
                  <p className="bubble__text">{entry.text}</p>
                </div>
              ),
            )
          )}

          {busy ? (
            <p className="chat__working" role="status">
              <span className="chat__dots" aria-hidden="true">
                <i />
                <i />
                <i />
              </span>
              Working on it…
            </p>
          ) : null}
        </div>

        {pending !== null ? (
          <ConfirmGate
            pending={pending}
            actor={actor}
            actorId={actorId}
            busy={busy}
            onAnswer={answer}
          />
        ) : null}

        <form className="chat__composer" onSubmit={submit}>
          <label className="sr-only" htmlFor="chat-input">
            Message the calendar
          </label>
          <textarea
            id="chat-input"
            ref={inputRef}
            className="chat__input"
            rows={2}
            value={draft}
            placeholder={pending === null ? "Type here…" : "Answer above to continue"}
            disabled={pending !== null}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={onInputKey}
          />
          <button
            type="submit"
            className="chat__send"
            disabled={busy || pending !== null || draft.trim().length === 0}
          >
            Send
          </button>
        </form>
      </aside>
    </>
  );
}
