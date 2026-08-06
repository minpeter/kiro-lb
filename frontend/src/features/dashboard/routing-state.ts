import type { AccountRoutingState } from "./types";

/**
 * States that take an account out of the pool until a human or a monthly reset
 * intervenes.
 *
 * Deliberately narrower than "renders as a destructive badge". `rate_limited`
 * and `cooling_down` are red too, but they clear on their own in seconds to
 * minutes, and they are exactly when rate history is worth reading - hiding a
 * rate-limited account from the rate chart would remove the evidence of the
 * thing being diagnosed. `uninitialized` stays as well: it is about to serve.
 */
const UNROUTABLE_STATES: ReadonlySet<string> = new Set([
  "suspended",
  // A rejected credential belongs here for the same reason as a suspension: it
  // cannot serve the next request and no amount of waiting changes that, so its
  // flat line would only crowd the chart. Only a re-login clears it.
  "auth_dead",
  "quota_exhausted",
  "quota_depleted",
]);

/**
 * Whether an account cannot serve the next request until something outside the
 * gateway changes.
 *
 * A null state means the series outlived the account it came from: rate
 * observations are kept for the whole window, so a deregistered account still
 * charts. Those stay visible - the history is the only remaining record of them,
 * and nothing is being claimed about routability.
 */
export function isUnroutable(state: AccountRoutingState | null): boolean {
  return state !== null && UNROUTABLE_STATES.has(state);
}
