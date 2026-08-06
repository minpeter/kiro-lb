import { describe, expect, it } from "vitest";
import { renderToString } from "react-dom/server";
import { RequestRateChart } from "./components/request-rate-chart";
import { isUnroutable } from "./routing-state";
import type { AccountRateSeries, AccountRoutingState, RequestRate } from "./types";

function series(account: string, routingState: AccountRoutingState | null): AccountRateSeries {
  return {
    account,
    routingState,
    success: [1, 2],
    rateLimited: [0, 0],
    failure: [0, 0],
    peakRpm: [1, 2],
    limitRpm: null,
    limitUnknownReason: "no rejections yet",
    safeRpm: 2,
    limitPrecisionRpm: null,
    rateLimitSamples: 0,
    informativeSamples: 0,
    estimateWindowSeconds: 3600,
  };
}

function rate(accounts: AccountRateSeries[]): RequestRate {
  return {
    bucketSeconds: 15,
    bucketStarts: [1_700_000_000, 1_700_000_015],
    rateWindowSeconds: 60,
    accounts,
  };
}

describe("isUnroutable", () => {
  it("hides states that need a human or a monthly reset", () => {
    expect(isUnroutable("suspended")).toBe(true);
    expect(isUnroutable("quota_exhausted")).toBe(true);
    expect(isUnroutable("quota_depleted")).toBe(true);
    // A rejected credential needs a re-login, so it charts a permanent flat line
    // exactly like a suspension.
    expect(isUnroutable("auth_dead")).toBe(true);
  });

  it("keeps states that clear on their own", () => {
    // These render as red badges too, but they recover in seconds to minutes -
    // and a rate-limited account is precisely what the rate chart is for.
    expect(isUnroutable("rate_limited")).toBe(false);
    expect(isUnroutable("cooling_down")).toBe(false);
    expect(isUnroutable("available")).toBe(false);
    expect(isUnroutable("uninitialized")).toBe(false);
  });

  it("keeps a series whose account is unknown", () => {
    expect(isUnroutable(null)).toBe(false);
  });
});

describe("RequestRateChart", () => {
  it("hides unroutable accounts and keeps the routable ones", () => {
    const html = renderToString(
      <RequestRateChart
        rate={rate([
          series("healthy00cafe", "available"),
          series("banned00beef", "suspended"),
          series("spent00feed", "quota_depleted"),
        ])}
        isLoading={false}
      />,
    );

    expect(html).toContain("healthy00cafe");
    expect(html).not.toContain("banned00beef");
    expect(html).not.toContain("spent00feed");
  });

  it("charts a rate-limited account rather than hiding it", () => {
    const html = renderToString(
      <RequestRateChart rate={rate([series("limited00cafe", "rate_limited")])} isLoading={false} />,
    );

    expect(html).toContain("limited00cafe");
  });

  it("discloses how many panels it hid", () => {
    const html = renderToString(
      <RequestRateChart
        rate={rate([series("healthy00cafe", "available"), series("banned00beef", "suspended")])}
        isLoading={false}
      />,
    );

    // Silently dropping a panel would leave an operator who knows the pool size
    // hunting for the missing account.
    expect(html).toContain("1 unroutable account hidden");
  });

  it("pluralizes the disclosure", () => {
    const html = renderToString(
      <RequestRateChart
        rate={rate([
          series("healthy00cafe", "available"),
          series("banned00beef", "suspended"),
          series("spent00feed", "quota_exhausted"),
        ])}
        isLoading={false}
      />,
    );

    expect(html).toContain("2 unroutable accounts hidden");
  });

  it("says the pool is unroutable rather than showing an empty card", () => {
    const html = renderToString(
      <RequestRateChart
        rate={rate([series("banned00beef", "suspended"), series("spent00feed", "quota_depleted")])}
        isLoading={false}
      />,
    );

    expect(html).toContain("No routable accounts");
    // Distinct from having no accounts at all, which means something different.
    expect(html).not.toContain("No accounts to chart");
  });

  it("keeps the no-accounts empty state when the pool really is empty", () => {
    const html = renderToString(<RequestRateChart rate={rate([])} isLoading={false} />);

    expect(html).toContain("No accounts to chart");
    expect(html).not.toContain("No routable accounts");
  });

  it("does not offer a toggle when nothing is hidden", () => {
    const html = renderToString(
      <RequestRateChart rate={rate([series("healthy00cafe", "available")])} isLoading={false} />,
    );

    expect(html).not.toContain("unroutable account");
  });
});
