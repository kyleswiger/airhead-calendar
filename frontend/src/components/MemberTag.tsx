import { safeColor } from "../lib/color";
import type { Member } from "../types";

interface MemberTagProps {
  member: Member | undefined;
  /** The id we were given, used as the label when the roster has no entry. */
  fallbackId?: string;
  size?: "normal" | "small";
}

/**
 * A member's color and their name, always together.
 *
 * PRD §11: never encode member identity in color alone. The swatch is
 * decorative and `aria-hidden`; the name is the actual label, so this renders
 * correctly for someone who cannot distinguish the colors at all.
 */
export function MemberTag({ member, fallbackId, size = "normal" }: MemberTagProps) {
  const name = member?.displayName ?? fallbackId ?? "Unassigned";
  const color = safeColor(member?.color);
  return (
    <span className={`member-tag member-tag--${size}`}>
      <span className="member-tag__swatch" style={{ background: color }} aria-hidden="true" />
      <span className="member-tag__name">{name}</span>
    </span>
  );
}
