import { describe, expect, it } from "vitest";
import { resetHint, usageIndicatorClass } from "./quota-display";
import type { Account } from "./types";

const baseAccount: Account = {
  id: "acc_test",
  initialized: true,
  routingState: "available",
  eligibleInSeconds: 0,
  requests: 0,
  failures: 0,
  cooldownSeconds: 0,
  deletable: false,
};

describe("usageIndicatorClass", () => {
  it("is destructive at full exhaustion, so the bar agrees with the quota badge", () => {
    expect(usageIndicatorClass(100)).toBe("bg-destructive");
    expect(usageIndicatorClass(95)).toBe("bg-destructive");
  });

  it("is warning in the 80-95 band, before the allowance is gone", () => {
    expect(usageIndicatorClass(94.9)).toBe("bg-warning");
    expect(usageIndicatorClass(80)).toBe("bg-warning");
  });

  it("stays primary below 80, where nothing is wrong yet", () => {
    expect(usageIndicatorClass(79.9)).toBe("bg-primary");
    expect(usageIndicatorClass(0)).toBe("bg-primary");
  });
});

describe("resetHint", () => {
  it("reports the countdown for a quota-gone account when one is known", () => {
    const account: Account = {
      ...baseAccount,
      routingState: "quota_exhausted",
      eligibleInSeconds: 6 * 86400 + 6 * 3600,
    };
    expect(resetHint(account)).toBe("resets in 6d 6h");
  });

  it("treats spent and exhausted identically, since both exclude the account", () => {
    const account: Account = {
      ...baseAccount,
      routingState: "quota_depleted",
      eligibleInSeconds: 7200,
    };
    expect(resetHint(account)).toBe("resets in 2h 0m");
  });

  it("says so plainly when a quota-gone account has no known reset", () => {
    const account: Account = {
      ...baseAccount,
      routingState: "quota_exhausted",
      eligibleInSeconds: 0,
    };
    expect(resetHint(account)).toBe("until it resets");
  });

  it("frames a self-clearing exclusion as a return, not a reset", () => {
    const account: Account = {
      ...baseAccount,
      routingState: "rate_limited",
      eligibleInSeconds: 300,
    };
    expect(resetHint(account)).toBe("back in 5m");
  });

  it("is null for a ready account, so no second reset source appears", () => {
    expect(resetHint(baseAccount)).toBeNull();
  });
});
