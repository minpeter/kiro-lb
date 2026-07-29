import type { AccountUsage } from "./types";

/** Format a unix timestamp for operator-facing tables. */
export function formatTimestamp(value?: number | null): string {
  return value ? new Date(value * 1000).toLocaleString() : "—";
}

/** Compact uptime rendering: 45m, 3h 20m, 2d 5h. */
export function formatDuration(seconds?: number | null): string {
  if (seconds == null) return "—";
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

/** Credit usage is fractional, so keep two decimals on the used value. */
export function formatUsage(usage?: AccountUsage | null): string {
  if (!usage || usage.usagePercent == null) return "—";
  const used = (usage.currentUsage ?? 0).toLocaleString(undefined, { maximumFractionDigits: 2 });
  const limit = (usage.usageLimit ?? 0).toLocaleString(undefined, { maximumFractionDigits: 2 });
  return `${usage.usagePercent.toFixed(2)}% · ${used} / ${limit}`;
}

export function formatLatency(milliseconds?: number | null): string {
  return milliseconds == null ? "—" : `${milliseconds.toLocaleString()} ms`;
}
