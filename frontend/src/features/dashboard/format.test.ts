import { describe, expect, it } from "vitest";
import { formatTokens, shareOf, summarizeUsage } from "./format";
import type { KeyUsage } from "./types";

function row(model: string, promptTokens: number, completionTokens: number, requests = 1) {
  return { model, promptTokens, completionTokens, totalTokens: promptTokens + completionTokens, requests, updatedAt: 0 };
}

describe("summarizeUsage", () => {
  it("sums across every key, including revoked ones", () => {
    // A revoked key's history still counts toward what the gateway consumed, so
    // the overview must not filter by key state.
    const usage: KeyUsage = {
      root: [row("claude-opus-5", 100, 20, 2)],
      revoked_key: [row("claude-opus-5", 50, 10, 1)],
    };

    const totals = summarizeUsage(usage);

    expect(totals.promptTokens).toBe(150);
    expect(totals.completionTokens).toBe(30);
    expect(totals.totalTokens).toBe(180);
    expect(totals.requests).toBe(3);
  });

  it("merges the same model reported under different keys", () => {
    const usage: KeyUsage = {
      a: [row("claude-opus-5", 10, 1)],
      b: [row("claude-opus-5", 20, 2)],
    };

    const totals = summarizeUsage(usage);

    expect(totals.models).toHaveLength(1);
    expect(totals.models[0]).toMatchObject({ model: "claude-opus-5", totalTokens: 33, requests: 2 });
  });

  it("ranks models by total tokens, descending", () => {
    const usage: KeyUsage = {
      root: [row("small", 1, 1), row("big", 1000, 500), row("medium", 100, 50)],
    };

    expect(summarizeUsage(usage).models.map((m) => m.model)).toEqual(["big", "medium", "small"]);
  });

  it("keeps the per-model totals consistent with the grand total", () => {
    const usage: KeyUsage = {
      root: [row("a", 7, 3), row("b", 11, 5)],
      other: [row("a", 2, 1)],
    };

    const totals = summarizeUsage(usage);
    const summed = totals.models.reduce((sum, m) => sum + m.totalTokens, 0);

    // The donut divides by the grand total, so any drift here would make the
    // slices fail to fill the ring.
    expect(summed).toBe(totals.totalTokens);
  });

  it("returns zeroes rather than NaN for an empty payload", () => {
    const totals = summarizeUsage({});

    expect(totals).toMatchObject({ promptTokens: 0, completionTokens: 0, totalTokens: 0, requests: 0 });
    expect(totals.models).toEqual([]);
  });

  it("handles a key with no recorded models", () => {
    expect(summarizeUsage({ fresh_key: [] }).totalTokens).toBe(0);
  });
});

describe("shareOf", () => {
  it("computes a percentage", () => {
    expect(shareOf(25, 100)).toBe(25);
  });

  it("returns 0 instead of dividing by zero", () => {
    // An empty store would otherwise produce NaN and render as "NaN%".
    expect(shareOf(0, 0)).toBe(0);
  });
});

describe("formatTokens", () => {
  it("scales to K/M/B", () => {
    expect(formatTokens(991_600_000)).toBe("991.6M");
    expect(formatTokens(1_145_556_639)).toBe("1.1B");
    expect(formatTokens(524_436)).toBe("524.4K");
  });

  it("leaves small counts exact", () => {
    expect(formatTokens(884)).toBe("884");
    expect(formatTokens(0)).toBe("0");
  });

  it("renders an em dash for a missing value", () => {
    expect(formatTokens(null)).toBe("—");
    expect(formatTokens(undefined)).toBe("—");
  });
});
