import { isUnroutable } from "./routing-state";
import type { Account, Overview } from "./types";

type RoutingAccount = Pick<Account, "routingState" | "enabled">;
type RequestOverview = Pick<Overview, "requests24h" | "successes24h">;

export type OverviewKpis = {
  routableAccounts: {
    count: number;
    total: number;
    isCritical: boolean;
  };
  success: {
    label: string;
    isCritical: boolean;
  };
  maskAverageLatency: boolean;
};

export function deriveOverviewKpis(
  accounts: readonly RoutingAccount[],
  overview: RequestOverview,
): OverviewKpis {
  // Disabled accounts stay listed but cannot serve; counting them in either
  // the numerator or the total desyncs this KPI from the info panel, which
  // filters on `enabled` ("3 of 2" rows, an all-disabled pool reading healthy).
  // The backend marks them twice (routingState "disabled", enabled false), so
  // either signal excludes.
  const enabledAccounts = accounts.filter(
    (account) => account.enabled !== false && account.routingState !== "disabled",
  );
  const routableCount = enabledAccounts.filter((account) => !isUnroutable(account.routingState)).length;
  const totalAccounts = enabledAccounts.length;
  const hasAccounts = accounts.length > 0;
  const hasTraffic = overview.requests24h > 0;
  const successPercent = hasTraffic ? (overview.successes24h / overview.requests24h) * 100 : null;

  return {
    routableAccounts: {
      count: routableCount,
      total: totalAccounts,
      // A listed-but-empty pool (every account disabled) can serve nothing,
      // so it is critical just like a non-empty pool with no routable account.
      isCritical: hasAccounts && routableCount === 0,
    },
    success: {
      label:
        successPercent === null
          ? "— (0/0)"
          : `${Math.round(successPercent)}% (${overview.successes24h.toLocaleString()}/${overview.requests24h.toLocaleString()})`,
      isCritical: successPercent !== null && successPercent < 50,
    },
    maskAverageLatency: hasTraffic && overview.successes24h === 0,
  };
}
