import { describe, expect, it } from "vitest";
import { TIER_ROUTING_MODES, summarizeTiers, tierModeHelpKey, tierModeLabelKey } from "./tier-routing";
import type { TierRoutingDerived } from "./types";

const derived: TierRoutingDerived = {
  freeModels: ["claude-haiku-4.5", "glm-5"],
  accounts: [
    { label: "a1", tier: "free" },
    { label: "a2", tier: "free" },
    { label: "a3", tier: "paid" },
    { label: "a4", tier: "unknown" },
  ],
};

describe("summarizeTiers", () => {
  it("counts each tier and the derived free models", () => {
    expect(summarizeTiers(derived)).toEqual({
      freeAccounts: 2,
      paidAccounts: 1,
      unknownAccounts: 1,
      freeModels: ["claude-haiku-4.5", "glm-5"],
      inactive: false,
    });
  });

  it("reports the rules inactive while no free account has a catalog", () => {
    expect(summarizeTiers({ freeModels: [], accounts: [{ label: "a1", tier: "paid" }] }).inactive).toBe(true);
    expect(summarizeTiers({ freeModels: ["glm-5"], accounts: [{ label: "a1", tier: "unknown" }] }).inactive).toBe(
      true,
    );
  });

  it("survives an answer with no derived block", () => {
    expect(summarizeTiers(undefined).inactive).toBe(true);
    expect(summarizeTiers(undefined).freeModels).toEqual([]);
  });
});

describe("mode labels", () => {
  it("derives a key per mode the gateway offers", () => {
    expect(TIER_ROUTING_MODES.map(tierModeLabelKey)).toEqual([
      "settings.tierPaidOff",
      "settings.tierPaidSoft",
      "settings.tierPaidStrict",
    ]);
    expect(TIER_ROUTING_MODES.map(tierModeHelpKey)).toEqual([
      "settings.tierPaidHelpOff",
      "settings.tierPaidHelpSoft",
      "settings.tierPaidHelpStrict",
    ]);
  });
});
