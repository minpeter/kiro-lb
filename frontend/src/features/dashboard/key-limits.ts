export type LimitParse = { ok: true; value: number | null } | { ok: false };

const SUFFIX: Record<string, number> = { k: 1e3, m: 1e6, b: 1e9 };

/** An empty field means no limit; digits take an optional k, m or b suffix (5m = 5,000,000). */
export function parseLimit(text: string): LimitParse {
  const cleaned = text.trim().toLowerCase().replace(/[\s,_]/g, "");
  if (cleaned === "") return { ok: true, value: null };
  const match = /^(\d+(?:\.\d+)?)([kmb]?)$/.exec(cleaned);
  if (!match) return { ok: false };
  const [, digits, suffix] = match;
  if (suffix === "" && digits.includes(".")) return { ok: false };
  const value = Math.round(Number(digits) * (SUFFIX[suffix] ?? 1));
  if (!Number.isSafeInteger(value) || value < 1) return { ok: false };
  return { ok: true, value };
}

export function limitFieldValue(limit: number | null | undefined): string {
  return limit == null ? "" : String(limit);
}

export function limitReached(used: number, limit: number | null | undefined): boolean {
  return limit != null && used >= limit;
}

export function keyLimitReached(
  usage: { tokens: number; requests: number },
  key: { tokenLimit?: number | null; requestLimit?: number | null },
): boolean {
  return limitReached(usage.tokens, key.tokenLimit) || limitReached(usage.requests, key.requestLimit);
}
