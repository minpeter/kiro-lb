import type { RequestRate } from "./types";

export type RateTotals = {
  /** Requests per minute per bucket, summed over every account. */
  perMinute: number[];
  /** Same buckets split by outcome, for the stacked bands. */
  successPerMinute: number[];
  rateLimitedPerMinute: number[];
  failurePerMinute: number[];
  /** Raw request counts over the whole window. */
  requests: number;
  successes: number;
  rateLimited: number;
  failures: number;
  /** Highest and mean per-minute rate across the window. */
  peakPerMinute: number;
  meanPerMinute: number;
  /** Sum of each account's own peak RPM: what the pool sustained at once. */
  peakConcurrentRpm: number;
  bucketSeconds: number;
  bucketStarts: number[];
};

const EMPTY: RateTotals = {
  perMinute: [],
  successPerMinute: [],
  rateLimitedPerMinute: [],
  failurePerMinute: [],
  requests: 0,
  successes: 0,
  rateLimited: 0,
  failures: 0,
  peakPerMinute: 0,
  meanPerMinute: 0,
  peakConcurrentRpm: 0,
  bucketSeconds: 0,
  bucketStarts: [],
};

/**
 * Collapse the per-account series into one pool-wide rate.
 *
 * The API reports per account because that is how rate limits apply, but the
 * overview asks what the gateway as a whole is serving. Summing here rather than
 * adding an endpoint keeps this view and the Accounts tab reading the same
 * numbers.
 *
 * Counts are converted to a per-minute rate so the shape does not change when
 * the bucket width does: 2 requests in a 5s bucket is 24/min.
 */
export function summarizeRate(rate?: RequestRate): RateTotals {
  if (!rate || rate.bucketStarts.length === 0) return EMPTY;

  const buckets = rate.bucketStarts.length;
  const perMinuteFactor = rate.bucketSeconds > 0 ? 60 / rate.bucketSeconds : 0;

  const success = new Array<number>(buckets).fill(0);
  const rateLimited = new Array<number>(buckets).fill(0);
  const failure = new Array<number>(buckets).fill(0);

  for (const account of rate.accounts) {
    for (let index = 0; index < buckets; index += 1) {
      success[index] += account.success[index] ?? 0;
      rateLimited[index] += account.rateLimited[index] ?? 0;
      failure[index] += account.failure[index] ?? 0;
    }
  }

  const total = success.map((value, index) => value + rateLimited[index] + failure[index]);
  const scale = (values: number[]) => values.map((value) => value * perMinuteFactor);
  const sum = (values: number[]) => values.reduce((carry, value) => carry + value, 0);

  const requests = sum(total);
  const perMinute = scale(total);
  const windowSeconds = buckets * rate.bucketSeconds;

  return {
    perMinute,
    successPerMinute: scale(success),
    rateLimitedPerMinute: scale(rateLimited),
    failurePerMinute: scale(failure),
    requests,
    successes: sum(success),
    rateLimited: sum(rateLimited),
    failures: sum(failure),
    peakPerMinute: perMinute.length > 0 ? Math.max(...perMinute) : 0,
    // Averaged over the window, not over non-empty buckets: idle time is part of
    // the rate, and skipping it would overstate a quiet period. The window can be
    // zero-length if the API ever reports a zero bucket width, which would make
    // this Infinity and render as "Infinity/min".
    meanPerMinute: windowSeconds > 0 ? (requests / windowSeconds) * 60 : 0,
    // Each account's peak is its own sliding-window RPM, so the pool's ceiling is
    // their sum rather than the peak of the summed buckets.
    peakConcurrentRpm: rate.accounts.reduce(
      (carry, account) => carry + (account.peakRpm.length > 0 ? Math.max(...account.peakRpm) : 0),
      0,
    ),
    bucketSeconds: rate.bucketSeconds,
    bucketStarts: rate.bucketStarts,
  };
}

/** Accounts that took at least one 429 in the window, for the warning line. */
export function throttledAccounts(rate?: RequestRate): string[] {
  if (!rate) return [];
  return rate.accounts
    .filter((account) => account.rateLimited.some(Boolean))
    .map((account) => account.account);
}
