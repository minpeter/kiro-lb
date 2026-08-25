import { isUnroutable } from "./routing-state";
import type { Account, Overview } from "./types";

type RoutingAccount = Pick<Account, "routingState">;
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
  const routableCount = accounts.filter((account) => !isUnroutable(account.routingState)).length;
  const totalAccounts = accounts.length;
  const hasTraffic = overview.requests24h > 0;
  const successPercent = hasTraffic ? (overview.successes24h / overview.requests24h) * 100 : null;

  return {
    routableAccounts: {
      count: routableCount,
      total: totalAccounts,
      isCritical: totalAccounts > 0 && routableCount === 0,
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
