import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { TotalRateChart } from "./components/total-rate-chart";
import type { RequestRate } from "./types";

const idleRate: RequestRate = {
  bucketSeconds: 5,
  bucketStarts: [1_785_000_000, 1_785_000_005, 1_785_000_010],
  rateWindowSeconds: 15,
  accounts: [],
};

describe("TotalRateChart", () => {
  it("shows an empty state and keeps the summary when the window has no requests", () => {
    const html = renderToString(<TotalRateChart rate={idleRate} isLoading={false} />);

    expect(html).toContain("Total requests per minute across all accounts. Peak 0 per minute");
    expect(html).toContain("No requests in this window");
    expect(html).toContain("Requests appear here once traffic reaches the gateway.");
    expect(html).toContain("Peak");
    expect(html).toContain("Average");
    expect(html).toContain("Requests");
    expect(html).toContain("Rejected");
    expect(html).toContain("Failed");
    expect(html).toContain("Peak is the busiest bucket scaled to a minute");
    expect(html).not.toContain('aria-label="Chart"');
  });
});
