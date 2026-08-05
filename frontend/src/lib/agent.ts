/**
 * Pure helpers over an agent turn. No React, no fetch.
 *
 * Everything here is a *read* of what the server said. Nothing in this file
 * decides whether a write is allowed or whether a gate can be skipped - those
 * are the server's calls, and the display only reflects them.
 */

import type { AgentAction, PendingConfirmation } from "../types";

/**
 * Tools that only look at the calendar. The contract says `actions` reports
 * writes only, so this list exists to be defensive in the other direction: an
 * unknown tool is treated as a write and triggers a re-fetch, because a
 * needless refresh is cheap and a stale kitchen screen is the actual failure.
 */
const READ_TOOLS: ReadonlySet<string> = new Set(["get_agenda", "find_conflicts", "list_members"]);

/** The server's own word that the write landed. */
export function isApplied(action: AgentAction): boolean {
  return action.status === "ok" && !READ_TOOLS.has(action.tool);
}

/** True when the day on screen is now out of date and must be re-fetched. */
export function hasAppliedWrite(actions: readonly AgentAction[]): boolean {
  return actions.some(isApplied);
}

/**
 * Whether a gate destroys something. Only `delete_event` does today, but the
 * substring check means a future `delete_*` / `remove_*` tool is treated as
 * dangerous by default rather than getting the friendly two-button treatment.
 */
export function isDestructive(tool: string): boolean {
  return /delete|remove|purge/i.test(tool);
}

export interface ConfirmLabels {
  /** The affirmative. Deliberately never the word "OK" - it says what happens. */
  approve: string;
  reject: string;
}

export function confirmLabels(pending: PendingConfirmation): ConfirmLabels {
  if (isDestructive(pending.tool)) return { approve: "Delete it", reject: "Keep it" };
  if (pending.tool === "update_event") return { approve: "Change it", reject: "Leave it" };
  return { approve: "Yes, do it", reject: "No, cancel" };
}

/**
 * The message body that rides along with a `confirm` answer. The contract
 * requires `message` on every request, and a gate answer is still a turn, so
 * the human's decision is spelled out in words the transcript can show.
 */
export function confirmMessage(approved: boolean): string {
  return approved ? "Yes - go ahead." : "No - cancel that.";
}
