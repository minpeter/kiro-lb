import { describe, expect, it } from "vitest";
import { deriveOverviewKpis } from "./overview-kpis";
import type { AccountRoutingState } from "./types";

const accounts = (...routingStates: AccountRoutingState[]) => routingStates.map((routingState) => ({ routingState }));

describe("deriveOverviewKpis", () => {
  it("counts only accounts that can route the next request", () => {
    const result = deriveOverviewKpis(accounts("available", "rate_limited", "quota_exhausted", "auth_dead"), {
      requests24h: 3,
      successes24h: 2,
    });

    expect(result.routableAccounts).toEqual({ count: 2, total: 4, isCritical: false });
  });

  it("marks a non-empty pool with no routable accounts as critical", () => {
    const result = deriveOverviewKpis(accounts("quota_exhausted", "suspended"), {
      requests24h: 0,
      successes24h: 0,
    });

    expect(result.routableAccounts).toEqual({ count: 0, total: 2, isCritical: true });
  });

  it("formats the success ratio and marks rates below 50 percent as critical", () => {
    expect(
      deriveOverviewKpis(accounts(), { requests24h: 3, successes24h: 0 }).success,
    ).toEqual({ label: "0% (0/3)", isCritical: true });
    expect(
      deriveOverviewKpis(accounts(), { requests24h: 2, successes24h: 1 }).success,
    ).toEqual({ label: "50% (1/2)", isCritical: false });
  });

  it("keeps the no-traffic success ratio neutral", () => {
    expect(
      deriveOverviewKpis(accounts(), { requests24h: 0, successes24h: 0 }).success,
    ).toEqual({ label: "— (0/0)", isCritical: false });
  });

  it("masks average latency only when all recent requests failed", () => {
    expect(
      deriveOverviewKpis(accounts(), { requests24h: 3, successes24h: 0 }).maskAverageLatency,
    ).toBe(true);
    expect(
      deriveOverviewKpis(accounts(), { requests24h: 3, successes24h: 1 }).maskAverageLatency,
    ).toBe(false);
    expect(
      deriveOverviewKpis(accounts(), { requests24h: 0, successes24h: 0 }).maskAverageLatency,
    ).toBe(false);
  });
});
