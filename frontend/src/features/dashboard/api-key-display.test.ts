import { describe, expect, it } from "vitest";
import { API_KEY_NAME_MAX, formatKeyPrefix, normalizeKeyName } from "./api-key-display";

describe("formatKeyPrefix", () => {
  it("appends an ellipsis to a bare prefix", () => {
    expect(formatKeyPrefix("kbr_5143")).toBe("kbr_5143…");
  });

  it("does not double an ellipsis already present on the stored prefix", () => {
    expect(formatKeyPrefix("5143…")).toBe("5143…");
  });

  it("treats three ASCII dots as an existing ellipsis", () => {
    expect(formatKeyPrefix("5143...")).toBe("5143...");
  });

  it("ignores trailing whitespace before deciding", () => {
    expect(formatKeyPrefix("5143  ")).toBe("5143…");
  });
});

describe("normalizeKeyName", () => {
  it("trims surrounding whitespace", () => {
    expect(normalizeKeyName("  prod key  ")).toBe("prod key");
  });

  it("rejects a name that is empty after trimming", () => {
    expect(normalizeKeyName("   ")).toBeNull();
    expect(normalizeKeyName("")).toBeNull();
  });

  it("accepts a name at the maximum length", () => {
    const maxed = "a".repeat(API_KEY_NAME_MAX);
    expect(normalizeKeyName(maxed)).toBe(maxed);
  });

  it("rejects a name beyond the maximum length", () => {
    expect(normalizeKeyName("a".repeat(API_KEY_NAME_MAX + 1))).toBeNull();
  });

  it("exposes a 64-character maximum", () => {
    expect(API_KEY_NAME_MAX).toBe(64);
  });
});
