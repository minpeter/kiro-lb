import { describe, expect, it } from "vitest";
import { accountRateSeries, ditherPalette, rateChartConfig, rateChartRows, tokenPieConfig, tokenPieRows } from "./dither-series";
import type { RateTotals } from "./request-rate-totals";
import type { AccountRateSeries } from "./types";
import type { Slice } from "./token-slices";

function totals(over: Partial<RateTotals> = {}): RateTotals {
  return {
    perMinute: [12, 36, 24],
    successPerMinute: [12, 24, 24],
    rateLimitedPerMinute: [0, 12, 0],
    failurePerMinute: [0, 0, 0],
    requests: 6,
    successes: 5,
    rateLimited: 1,
    failures: 0,
    peakPerMinute: 36,
    meanPerMinute: 24,
    peakConcurrentRpm: 40,
    bucketSeconds: 5,
    bucketStarts: [1_785_000_000, 1_785_000_005, 1_785_000_010],
    ...over,
  };
}

function slice(label: string, tokens: number, share: number): Slice {
  return { label, tokens, share, color: "var(--chart-1)", models: 1 };
}

describe("rateChartRows", () => {
  it("emits one row per bucket carrying the served and rejected rates", () => {
    const rows = rateChartRows(totals());

    expect(rows).toHaveLength(3);
    expect(rows[1].served).toBe(36);
    expect(rows[1].rejected).toBe(12);
    expect(typeof rows[1].at).toBe("string");
  });

  it("labels each bucket with its own clock time", () => {
    const rows = rateChartRows(totals());

    expect(new Set(rows.map((row) => row.at)).size).toBeGreaterThan(0);
    expect(rows[0].at).not.toBe("");
  });

  it("returns no rows for an empty window", () => {
    expect(rateChartRows(totals({ perMinute: [], bucketStarts: [] }))).toEqual([]);
  });
});

describe("rateChartConfig", () => {
  it("configures only the served series while nothing was rejected", () => {
    const config = rateChartConfig(totals({ rateLimited: 0, rateLimitedPerMinute: [0, 0, 0] }));

    expect(Object.keys(config)).toEqual(["served"]);
    expect(config.served.color).toBe("blue");
  });

  it("adds a red rejected series once an upstream rejection lands", () => {
    const config = rateChartConfig(totals());

    expect(Object.keys(config)).toEqual(["served", "rejected"]);
    expect(config.rejected.color).toBe("red");
  });
});

describe("accountRateSeries", () => {
  const base: AccountRateSeries = {
    account: "a@example.com",
    success: [1, 2],
    rateLimited: [0, 0],
    failure: [0, 0],
    peakRpm: [12, 24],
    limitRpm: 60,
    limitUnknownReason: null,
    safeRpm: 30,
    limitPrecisionRpm: 30,
    rateLimitSamples: 2,
    informativeSamples: 2,
    estimateWindowSeconds: 21_600,
  };

  it("keeps the account's own peak-rpm buckets as the plotted series", () => {
    const view = accountRateSeries(base);

    expect(view.rows.map((row) => row.rpm)).toEqual([12, 24]);
    expect(view.peak).toBe(24);
  });

  it("stays blue below the warning fraction of the observed limit", () => {
    expect(accountRateSeries(base).color).toBe("blue");
  });

  it("turns red once traffic reaches 80% of the observed limit", () => {
    const view = accountRateSeries({ ...base, peakRpm: [12, 48] });

    expect(view.nearLimit).toBe(true);
    expect(view.color).toBe("red");
  });

  it("has no load factor without an observed limit", () => {
    const view = accountRateSeries({ ...base, limitRpm: null });

    expect(view.load).toBeNull();
    expect(view.nearLimit).toBe(false);
  });

  it("reports an idle account as having no traffic", () => {
    const view = accountRateSeries({ ...base, peakRpm: [0, 0], success: [0, 0] });

    expect(view.hasTraffic).toBe(false);
  });

  it("lifts the y-domain above the observed limit so the guide stays inside the plot", () => {
    const view = accountRateSeries(base);

    expect(view.yMax).toBeCloseTo(75, 5);
  });

  it("leaves the y-domain to the data when no limit has been observed", () => {
    expect(accountRateSeries({ ...base, limitRpm: null }).yMax).toBeUndefined();
  });
});

describe("tokenPieRows / tokenPieConfig", () => {
  const slices = [slice("claude-sonnet-4-5", 900, 90), slice("gpt-5", 100, 10)];

  it("keys each row by its slice id and carries the token count as the value", () => {
    const rows = tokenPieRows(slices);

    expect(rows).toEqual([
      { slice: "s0", tokens: 900 },
      { slice: "s1", tokens: 100 },
    ]);
  });

  it("maps every slice id to a dither colour and its model label", () => {
    const config = tokenPieConfig(slices);

    expect(Object.keys(config)).toEqual(["s0", "s1"]);
    expect(config.s0.label).toBe("claude-sonnet-4-5");
    expect(ditherPalette).toContain(config.s0.color);
    expect(config.s1.color).not.toBe(config.s0.color);
  });

  it("paints the grouped tail grey so it does not read as a model", () => {
    const config = tokenPieConfig([...slices, slice("other models", 5, 0.5)]);

    expect(config.s2.color).toBe("grey");
  });

  it("drops zero-token slices, which would draw no wedge", () => {
    const rows = tokenPieRows([...slices, slice("empty", 0, 0)]);

    expect(rows.map((row) => row.slice)).toEqual(["s0", "s1"]);
  });
});
