import { describe, expect, it } from "vitest";
import { renderToString } from "react-dom/server";
import { AccountRateChart, AccountRateDetails } from "./components/account-rate-chart";
import { AccountTokenPanel } from "./components/account-token-panel";
import { accountTokenRows } from "./account-token-rows";
import { isUnroutable } from "./routing-state";
import type { AccountRateSeries, AccountRoutingState, AccountTokenUsage, RequestRate } from "./types";

function series(account: string, routingState: AccountRoutingState | null = "available"): AccountRateSeries {
  return {
    account,
    routingState,
    success: [1, 2],
    rateLimited: [0, 0],
    failure: [0, 0],
    peakRpm: [3, 7],
    limitRpm: null,
    limitUnknownReason: "no rate rejection observed yet",
    safeRpm: 7,
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
  it.each(["suspended", "auth_dead", "account_issue", "quota_exhausted", "quota_depleted"] as const)(
    "hides %s until a human or monthly reset intervenes",
    (state) => expect(isUnroutable(state)).toBe(true),
  );

  it.each(["rate_limited", "cooling_down", "available", "uninitialized", null] as const)(
    "keeps rate history for %s",
    (state) => expect(isUnroutable(state)).toBe(false),
  );
});

describe("AccountRateChart", () => {
  it("keeps peak and rejections compact, with an accessible observed limit", () => {
    const html = renderToString(<AccountRateChart series={{
      ...series("account-a"), peakRpm: [3, 9], limitRpm: 10, rateLimited: [1, 2],
    }} />);
    expect(html).toContain("9/min peak");
    expect(html).toContain("3 rejected");
    expect(html).toContain("observed limit 10 per minute");
    expect(html).not.toContain("Approaching the observed limit");
    expect(html).not.toContain("90% of limit");
  });

  it("keeps the unknown-limit explanation in the details", () => {
    const html = renderToString(<AccountRateDetails series={series("account-a")} />);
    expect(html).toContain("no rate rejection observed yet");
    expect(html).toContain("Served 7/min without rejection");
  });

  it("keeps load, rejections and the near-limit warning in the details", () => {
    const html = renderToString(<AccountRateDetails series={{
      ...series("account-a"), peakRpm: [3, 9], limitRpm: 10, rateLimited: [1, 2],
    }} />);
    expect(html).toContain("90% of limit");
    expect(html).toContain("3 rejected");
    expect(html).toContain("Approaching the observed limit");
    expect(html).toContain("~10/min");
  });

  it("keeps idle accounts visible without claiming a measured rate limit", () => {
    const html = renderToString(<AccountRateChart series={{ ...series("idle"), peakRpm: [0, 0] }} />);
    expect(html).toContain("No traffic in this window");
    expect(html).toContain("Idle");
    expect(html).toContain("0/min peak");
    expect(html).not.toContain("of limit");
  });
});

describe("combined account usage", () => {
  const usage: AccountTokenUsage = {
    historical: { email: "same@example.com", models: [], totalTokens: 200, requests: 2 },
    active: { email: "same@example.com", models: [], totalTokens: 800, requests: 8 },
  };

  it("joins by account ID, retains historical usage, and adds rate-only accounts without changing totals", () => {
    const rows = accountTokenRows(usage, rate([series("new"), series("active")]));
    expect(rows.map((row) => row.account)).toEqual(["active", "historical", "new"]);
    expect(rows[0].rate?.account).toBe("active");
    expect(rows[1].rate).toBeUndefined();
    expect(rows[2]).toMatchObject({ totalTokens: 0, promptTokens: 0, completionTokens: 0, requests: 0, models: [] });
    expect(rows.reduce((sum, row) => sum + row.totalTokens, 0)).toBe(1000);
    expect(rows.reduce((sum, row) => sum + row.requests, 0)).toBe(10);
  });

  it("hides only the unroutable graph, discloses it, and keeps its token usage", () => {
    const html = renderToString(<AccountTokenPanel accountTokenUsage={usage}
      rate={rate([series("active", "suspended"), series("limited", "rate_limited")])} isLoading={false} />);
    expect(html).toContain("active");
    expect(html).toContain("800");
    expect(html).toContain("80.0%");
    expect(html).toContain("Temporary suspension");
    expect(html).toContain("Show request history");
    expect(html).not.toContain("Peak requests per minute for account active:");
    expect(html).toContain("Peak requests per minute for account limited:");
    expect(html).toMatch(/Accounts used<\/dt><dd[^>]*>2<\/dd>/);
  });

  it("shows traffic before any tokens have been recorded, with zero shares and zero accounts used", () => {
    const html = renderToString(<AccountTokenPanel accountTokenUsage={{}}
      rate={rate([series("new")])} isLoading={false} />);
    expect(html).toContain("Peak requests per minute for account new:");
    expect(html).not.toContain("No tokens recorded yet");
    expect(html).not.toContain("100%");
    expect(html).not.toContain("&lt;0.1%");
    expect(html).toMatch(/Accounts used<\/dt><dd[^>]*>0<\/dd>/);
  });

  it("distinguishes missing rate history from an observed idle window", () => {
    const html = renderToString(<AccountTokenPanel accountTokenUsage={usage}
      rate={rate([{ ...series("active"), peakRpm: [0, 0] }])} isLoading={false} />);
    expect(html).toContain("No recent request history");
    expect(html).toContain("No traffic in this window");
    expect(html).toContain("15s buckets");
    expect(html).not.toContain("Per-account request rate");
  });
});
