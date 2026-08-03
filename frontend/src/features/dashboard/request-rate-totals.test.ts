import { describe, expect, it } from "vitest";
import { summarizeRate, throttledAccounts } from "./request-rate-totals";
import type { AccountRateSeries, RequestRate } from "./types";

function series(account: string, over: Partial<AccountRateSeries> = {}): AccountRateSeries {
  const buckets = over.success?.length ?? 4;
  const zeros = () => new Array<number>(buckets).fill(0);
  return {
    account,
    success: zeros(),
    rateLimited: zeros(),
    failure: zeros(),
    peakRpm: zeros(),
    limitRpm: null,
    limitUnknownReason: null,
    safeRpm: 0,
    limitPrecisionRpm: null,
    rateLimitSamples: 0,
    informativeSamples: 0,
    estimateWindowSeconds: 0,
    ...over,
  };
}

function rate(accounts: AccountRateSeries[], bucketSeconds = 5, buckets = 4): RequestRate {
  return {
    bucketSeconds,
    bucketStarts: Array.from({ length: buckets }, (_, index) => 1_785_000_000 + index * bucketSeconds),
    rateWindowSeconds: 60,
    accounts,
  };
}

describe("summarizeRate", () => {
  it("sums every account into one series", () => {
    const totals = summarizeRate(
      rate([
        series("a", { success: [1, 0, 2, 0] }),
        series("b", { success: [0, 3, 1, 0] }),
      ]),
    );

    // 5s buckets, so one request is 12/min.
    expect(totals.perMinute).toEqual([12, 36, 36, 0]);
    expect(totals.requests).toBe(7);
  });

  it("converts counts to a per-minute rate from the bucket width", () => {
    // The same two requests must read the same regardless of bucket width.
    const fiveSecond = summarizeRate(rate([series("a", { success: [2, 0, 0, 0] })], 5));
    const fifteenSecond = summarizeRate(rate([series("a", { success: [6, 0, 0, 0] })], 15));

    expect(fiveSecond.perMinute[0]).toBe(24);
    expect(fifteenSecond.perMinute[0]).toBe(24);
  });

  it("keeps the outcome bands separate", () => {
    const totals = summarizeRate(
      rate([
        series("a", { success: [1, 0, 0, 0], rateLimited: [0, 1, 0, 0], failure: [0, 0, 1, 0] }),
        series("b", { rateLimited: [0, 1, 0, 0] }),
      ]),
    );

    expect(totals.successes).toBe(1);
    expect(totals.rateLimited).toBe(2);
    expect(totals.failures).toBe(1);
    // Every outcome counts toward the total: a rejected request was still made.
    expect(totals.requests).toBe(4);
  });

  it("reports the peak of the combined series, not of one account", () => {
    // Neither account peaks at 3 alone; the pool does.
    const totals = summarizeRate(
      rate([series("a", { success: [2, 0, 0, 0] }), series("b", { success: [1, 0, 0, 0] })]),
    );

    expect(totals.peakPerMinute).toBe(36);
  });

  it("averages over the whole window, including idle buckets", () => {
    // 4 requests over 4 x 5s = 20s is 12/min. Averaging only the busy bucket
    // would claim 48/min and make a quiet period look saturated.
    const totals = summarizeRate(rate([series("a", { success: [4, 0, 0, 0] })]));

    expect(totals.meanPerMinute).toBe(12);
  });

  it("sums each account's own peak for the concurrent ceiling", () => {
    // peakRpm is already a sliding-window rate per account, so the pool's
    // sustained ceiling is their sum rather than the peak of summed buckets.
    const totals = summarizeRate(
      rate([series("a", { peakRpm: [13, 0, 0, 0] }), series("b", { peakRpm: [0, 7, 0, 0] })]),
    );

    expect(totals.peakConcurrentRpm).toBe(20);
  });

  it("returns zeroes for an absent payload", () => {
    const totals = summarizeRate(undefined);

    expect(totals.requests).toBe(0);
    expect(totals.perMinute).toEqual([]);
    expect(totals.peakPerMinute).toBe(0);
    expect(totals.meanPerMinute).toBe(0);
  });

  it("handles a pool with no accounts", () => {
    const totals = summarizeRate(rate([]));

    // Buckets still exist, so the chart renders a flat line rather than breaking.
    expect(totals.perMinute).toEqual([0, 0, 0, 0]);
    expect(totals.requests).toBe(0);
  });

  it("does not divide by zero on a zero-width bucket", () => {
    const totals = summarizeRate(rate([series("a", { success: [1, 0, 0, 0] })], 0));

    expect(totals.perMinute.every(Number.isFinite)).toBe(true);
    expect(Number.isFinite(totals.meanPerMinute)).toBe(true);
  });

  it("tolerates an account whose arrays are shorter than the bucket list", () => {
    // Defensive: a series that lags the bucket window must not produce NaN.
    const payload = rate([series("a", { success: [1, 2] })], 5, 4);

    const totals = summarizeRate(payload);

    expect(totals.perMinute.every(Number.isFinite)).toBe(true);
    expect(totals.requests).toBe(3);
  });

  it("carries the bucket metadata through for axis labels", () => {
    const payload = rate([series("a")], 15, 3);

    const totals = summarizeRate(payload);

    expect(totals.bucketSeconds).toBe(15);
    expect(totals.bucketStarts).toEqual(payload.bucketStarts);
  });
});

describe("throttledAccounts", () => {
  it("names only the accounts that took a rejection", () => {
    const payload = rate([
      series("healthy", { success: [5, 0, 0, 0] }),
      series("throttled", { rateLimited: [0, 1, 0, 0] }),
    ]);

    expect(throttledAccounts(payload)).toEqual(["throttled"]);
  });

  it("is empty when nothing was rejected", () => {
    expect(throttledAccounts(rate([series("a", { success: [1, 0, 0, 0] })]))).toEqual([]);
  });

  it("is empty for an absent payload", () => {
    expect(throttledAccounts(undefined)).toEqual([]);
  });
});
