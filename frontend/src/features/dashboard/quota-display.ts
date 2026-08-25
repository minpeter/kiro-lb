import { formatDuration } from "./format";
import type { Account } from "./types";

/**
 * Progress bar color for a usage percentage, matching the severity ladder the
 * state badge already uses: at 95%+ the allowance is effectively gone and the
 * bar must agree with the adjacent destructive "quota exhausted" badge rather
 * than contradicting it in calm blue. The 80% warning band gives one earlier
 * glanceable signal before that point.
 */
export function usageIndicatorClass(usagePercent: number): string {
  if (usagePercent >= 95) return "bg-destructive";
  if (usagePercent >= 80) return "bg-warning";
  return "bg-primary";
}

/**
 * The single reset display for an account row.
 *
 * Reset information used to be split across two disagreeing sources: a Reset
 * column reading `daysUntilReset` (an em dash on every polled row) while the
 * state badge hinted at `eligibleInSeconds`. The countdown is the concrete
 * value, so it is the one that survives; the column is gone and this hint is
 * the only place reset timing is stated. Quota-gone accounts "reset";
 * self-clearing exclusions (rate limits, cooldowns) come "back", and a ready
 * account has nothing to say.
 */
export function resetHint(account: Account): string | null {
  const quotaGone = account.routingState === "quota_exhausted" || account.routingState === "quota_depleted";
  if (quotaGone) {
    return account.eligibleInSeconds > 0 ? `resets in ${formatDuration(account.eligibleInSeconds)}` : "until it resets";
  }
  return account.eligibleInSeconds > 0 ? `back in ${formatDuration(account.eligibleInSeconds)}` : null;
}
