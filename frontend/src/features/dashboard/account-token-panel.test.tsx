import { describe, expect, it } from "bun:test";
import { renderToString } from "react-dom/server";
import { AccountTokenPanel } from "./components/account-token-panel";
import type { AccountTokenUsage, KeyModelUsage } from "./types";

function model(name: string, prompt: number, completion: number, requests = 1): KeyModelUsage {
  return {
    model: name,
    promptTokens: prompt,
    completionTokens: completion,
    totalTokens: prompt + completion,
    requests,
    generationSeconds: 0,
    tokensPerSecond: null,
    updatedAt: 1,
  };
}

const usage: AccountTokenUsage = {
  ff3b89220c77: {
    email: "spender@example.com",
    models: [model("claude-sonnet-4.5", 900, 100, 9)],
    totalTokens: 1000,
    requests: 9,
  },
  "58e33ab2f014": {
    email: null,
    models: [model("claude-haiku-4.5", 50, 50, 1)],
    totalTokens: 100,
    requests: 1,
  },
};

describe("AccountTokenPanel", () => {
  it("shows an email when one is known", () => {
    const html = renderToString(<AccountTokenPanel accountTokenUsage={usage} isLoading={false} />);
    expect(html).toContain("spender@example.com");
  });

  it("falls back to the hashed label when no email is known", () => {
    const html = renderToString(<AccountTokenPanel accountTokenUsage={usage} isLoading={false} />);
    expect(html).toContain("58e33ab2f014");
  });

  it("reports each account's share of the total", () => {
    // 1000 of 1100 tokens: the operator's first question is which account is
    // carrying the pool, so the share has to be on the row.
    const html = renderToString(<AccountTokenPanel accountTokenUsage={usage} isLoading={false} />);
    expect(html).toContain("90.9%");
  });

  it("totals input and output across accounts", () => {
    const html = renderToString(<AccountTokenPanel accountTokenUsage={usage} isLoading={false} />);
    // 950 input + 150 output = 1.1K total.
    expect(html).toContain("Accounts used");
    expect(html).toContain("2");
  });

  it("states that unattributed history cannot be recovered", () => {
    // The empty state has to say this: tokens recorded before the account axis
    // existed cannot be backfilled, and an operator seeing zero would otherwise
    // assume the feature is broken.
    const html = renderToString(<AccountTokenPanel accountTokenUsage={{}} isLoading={false} />);
    expect(html).toContain("cannot be attributed");
  });

  it("does not claim the numbers are Kiro's quota accounting", () => {
    // They are local tiktoken estimates. Presenting them as the upstream's own
    // billing would be wrong, and the two do not move together: quota counts
    // requests, not tokens.
    const html = renderToString(<AccountTokenPanel accountTokenUsage={usage} isLoading={false} />);
    expect(html).toContain("not Kiro");
  });
});
