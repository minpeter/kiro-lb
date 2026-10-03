import type { TierRoutingDerived, TierRoutingMode } from "./types";

/** Order the mode select offers; the gateway returns the same list. */
export const TIER_ROUTING_MODES: TierRoutingMode[] = ["off", "soft", "strict"];

export function tierModeLabelKey(mode: TierRoutingMode): string {
  return `settings.tierPaid${mode.charAt(0).toUpperCase()}${mode.slice(1)}`;
}

export function tierModeHelpKey(mode: TierRoutingMode): string {
  return `settings.tierPaidHelp${mode.charAt(0).toUpperCase()}${mode.slice(1)}`;
}

export interface TierSummary {
  freeAccounts: number;
  paidAccounts: number;
  unknownAccounts: number;
  freeModels: string[];
  /** True while neither rule can act: no free account has reported a catalog. */
  inactive: boolean;
}

export function summarizeTiers(derived: TierRoutingDerived | undefined): TierSummary {
  const accounts = derived?.accounts ?? [];
  const freeModels = derived?.freeModels ?? [];
  const count = (tier: string) => accounts.filter((a) => a.tier === tier).length;
  const freeAccounts = count("free");
  return {
    freeAccounts,
    paidAccounts: count("paid"),
    unknownAccounts: count("unknown"),
    freeModels,
    inactive: freeAccounts === 0 || freeModels.length === 0,
  };
}
