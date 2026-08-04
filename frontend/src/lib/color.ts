/** Pure color helpers. Member colors arrive from the API as hex. */

const HEX = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i;

const FALLBACK = "#8b93a7";

export interface Rgb {
  r: number;
  g: number;
  b: number;
}

export function parseHex(hex: string): Rgb | null {
  const m = HEX.exec(hex.trim());
  const body = m?.[1];
  if (body === undefined) return null;
  const full =
    body.length === 3
      ? body
          .split("")
          .map((c) => c + c)
          .join("")
      : body;
  const n = Number.parseInt(full, 16);
  if (!Number.isFinite(n)) return null;
  return { r: (n >> 16) & 0xff, g: (n >> 8) & 0xff, b: n & 0xff };
}

/** Tinted fills for bands and chips without shipping a color library. */
export function withAlpha(hex: string, alpha: number): string {
  const rgb = parseHex(hex) ?? parseHex(FALLBACK);
  if (!rgb) return hex;
  const a = Math.min(1, Math.max(0, alpha));
  return `rgba(${rgb.r}, ${rgb.g}, ${rgb.b}, ${a})`;
}

/** A usable color even if a member row arrives without one. */
export function safeColor(hex: string | undefined): string {
  if (hex === undefined) return FALLBACK;
  return parseHex(hex) === null ? FALLBACK : hex;
}
