import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { RequestLogDetailFields, RequestLogTable } from "./components/request-log-table";
import type { RequestLogDetail, RequestLogPage } from "./types";

const emptyHandlers = {
  onLimitChange: () => undefined,
  onOffsetChange: () => undefined,
  onModelChange: () => undefined,
  onOrderChange: () => undefined,
};

function page(credits: number | null, modelMultiplier?: number | null): RequestLogPage {
  return {
    logs: [
      {
        id: 1,
        created_at: 1_777_000_000,
        route: "/v1/chat/completions",
        model: "gpt-5.6-sol",
        status_code: 200,
        latency_ms: 1200,
        credits,
        modelMultiplier,
      },
    ],
    total: 1,
    limit: 25,
    offset: 0,
    hasMore: false,
    models: ["gpt-5.6-sol"],
  };
}

const solLongContext: RequestLogDetail = {
  id: 1,
  createdAt: 1_777_000_000,
  route: "/v1/chat/completions",
  model: "gpt-5.6-sol",
  statusCode: 200,
  latencyMs: 1200,
  clientIp: "203.0.113.10",
  userAgent: "curl/8.0",
  inputTokens: 400_000,
  outputTokens: 12,
  creditsSpent: 8.8,
  modelMultiplier: 8.8,
};

describe("RequestLogTable", () => {
  it("labels list spend as credits instead of a multiplier", () => {
    const html = renderToString(
      <RequestLogTable page={page(8.8)} isLoading={false} model="" order="newest" {...emptyHandlers} />,
    );

    expect(html).toContain("gpt-5.6-sol");
    expect(html).toContain("8.8 credits");
    expect(html).toContain("Credits spent");
    expect(html).not.toContain("8.8x");
  });

  it("shows a 0.03 spend as credits, not 0.03x", () => {
    const html = renderToString(
      <RequestLogTable page={page(0.03)} isLoading={false} model="" order="newest" {...emptyHandlers} />,
    );

    expect(html).toContain("0.03 credits");
    expect(html).not.toContain("0.03x");
  });

  it("renders the multiplier separately when the list payload includes it", () => {
    const html = renderToString(
      <RequestLogTable page={page(8.8, 4.4)} isLoading={false} model="" order="newest" {...emptyHandlers} />,
    );

    expect(html).toContain("8.8 credits");
    expect(html).toContain("4.4x");
    expect(html).toContain("Model multiplier");
  });

  it("omits the spend mark when credits are absent", () => {
    const html = renderToString(
      <RequestLogTable page={page(null)} isLoading={false} model="" order="newest" {...emptyHandlers} />,
    );

    expect(html).toContain("gpt-5.6-sol");
    expect(html).not.toContain("credits");
  });
});

describe("RequestLogDetailFields", () => {
  it("renders credits spent and the tier-aware multiplier when present", () => {
    const html = renderToString(<RequestLogDetailFields detail={solLongContext} />);

    expect(html).toContain("Credits spent");
    expect(html).toContain("8.8");
    expect(html).toContain("Model multiplier");
    expect(html).toContain("8.8x");
    expect(html).toContain("400,000 / 12");
  });

  it("hides the cost fields when the payload has neither figure", () => {
    const html = renderToString(
      <RequestLogDetailFields detail={{ ...solLongContext, creditsSpent: null, modelMultiplier: null }} />,
    );

    expect(html).not.toContain("Credits spent");
    expect(html).not.toContain("Model multiplier");
  });
});
