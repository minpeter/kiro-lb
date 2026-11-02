import { describe, expect, it } from "vitest";
import { keyLimitReached, limitFieldValue, limitReached, parseLimit } from "./key-limits";

describe("parseLimit", () => {
  it("treats an empty field as no limit", () => {
    expect(parseLimit("")).toEqual({ ok: true, value: null });
    expect(parseLimit("   ")).toEqual({ ok: true, value: null });
  });

  it("reads plain whole numbers, ignoring separators", () => {
    expect(parseLimit("1000")).toEqual({ ok: true, value: 1000 });
    expect(parseLimit("1,000,000")).toEqual({ ok: true, value: 1_000_000 });
    expect(parseLimit(" 1 000 ")).toEqual({ ok: true, value: 1000 });
  });

  it("expands k, m and b suffixes", () => {
    expect(parseLimit("500k")).toEqual({ ok: true, value: 500_000 });
    expect(parseLimit("5M")).toEqual({ ok: true, value: 5_000_000 });
    expect(parseLimit("1.5m")).toEqual({ ok: true, value: 1_500_000 });
    expect(parseLimit("2b")).toEqual({ ok: true, value: 2_000_000_000 });
  });

  it("rejects zero, fractions, negatives, letters and overflow", () => {
    for (const bad of ["0", "0k", "1.5", "-5", "abc", "10x", "1.2.3", "99999999999999999999"]) {
      expect(parseLimit(bad), bad).toEqual({ ok: false });
    }
  });
});

describe("limits in the key list", () => {
  it("fills the edit field from a stored limit", () => {
    expect(limitFieldValue(null)).toBe("");
    expect(limitFieldValue(undefined)).toBe("");
    expect(limitFieldValue(2500)).toBe("2500");
  });

  it("flags a limit once usage meets it", () => {
    expect(limitReached(99, 100)).toBe(false);
    expect(limitReached(100, 100)).toBe(true);
    expect(limitReached(1_000_000, null)).toBe(false);
  });

  it("flags a key when either limit is met", () => {
    const usage = { tokens: 50, requests: 10 };
    expect(keyLimitReached(usage, { tokenLimit: 100, requestLimit: 10 })).toBe(true);
    expect(keyLimitReached(usage, { tokenLimit: 100, requestLimit: 11 })).toBe(false);
    expect(keyLimitReached(usage, {})).toBe(false);
  });
});
