import type { AccountUsage, KeyUsage } from "./types";

// Pinned to en-US like the token formatter below: operator tables read one
// locale, and seconds add noise the eye never parses at a glance.
const absoluteTimestamp = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  year: "numeric",
  hour: "numeric",
  minute: "2-digit",
});

/** Format a unix timestamp (seconds) for operator-facing tables: Aug 25, 2026, 9:24 PM. */
export function formatTimestamp(value?: number | null): string {
  return value ? absoluteTimestamp.format(new Date(value * 1000)) : "—";
}

const MINUTE = 60;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;
const WEEK = 7 * DAY;

/**
 * Relative age of a unix timestamp (seconds) against `nowMs` (milliseconds,
 * i.e. Date.now()). Recent rows read faster as "2m ago" than as a wall-clock
 * time; past a week the absolute date is more useful than "14d ago". Future
 * deltas (clock skew) clamp to "just now" rather than rendering a negative.
 */
export function formatRelativeTime(nowMs: number, value?: number | null): string {
  if (value == null) return "—";
  const deltaSeconds = Math.floor((nowMs - value * 1000) / 1000);
  if (deltaSeconds < MINUTE) return "just now";
  if (deltaSeconds < HOUR) return `${Math.floor(deltaSeconds / MINUTE)}m ago`;
  if (deltaSeconds < DAY) return `${Math.floor(deltaSeconds / HOUR)}h ago`;
  if (deltaSeconds < WEEK) return `${Math.floor(deltaSeconds / DAY)}d ago`;
  return formatTimestamp(value);
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

// Pinned to en-US on purpose: the K/M/B units are what operators read these
// tables for, and the browser locale would substitute its own scale (ko-KR
// renders 991,600,000 as 9.9억).
const compactTokens = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });

/** Token counts run to nine digits, which is unreadable in a table: 991.6M. */
export function formatTokens(value?: number | null): string {
  if (value == null) return "—";
  return compactTokens.format(value);
}

/** Exact count for the cell's title, so the rounded figure stays verifiable. */
export function exactTokens(value?: number | null): string | undefined {
  return value == null ? undefined : `${value.toLocaleString()} tokens`;
}

export type ModelTotal = {
  model: string;
  promptTokens: number;
  completionTokens: number;
  totalTokens: number;
  requests: number;
};

export type UsageTotals = {
  promptTokens: number;
  completionTokens: number;
  totalTokens: number;
  requests: number;
  /** Descending by total tokens, so the caller can take a meaningful head. */
  models: ModelTotal[];
};

/**
 * Collapse the per-key breakdown into grand totals plus a per-model ranking.
 *
 * The API returns usage keyed by API key, because that is how it is attributed.
 * The overview asks the other question: what has this gateway spent in total,
 * regardless of who spent it. Summing across keys here keeps that view honest
 * when keys are added or revoked, since a revoked key's history still counts
 * toward what was consumed.
 */
export function summarizeUsage(usage: KeyUsage): UsageTotals {
  const byModel = new Map<string, ModelTotal>();
  let promptTokens = 0;
  let completionTokens = 0;
  let requests = 0;

  for (const rows of Object.values(usage)) {
    for (const row of rows) {
      promptTokens += row.promptTokens;
      completionTokens += row.completionTokens;
      requests += row.requests;
      const entry = byModel.get(row.model) ?? {
        model: row.model,
        promptTokens: 0,
        completionTokens: 0,
        totalTokens: 0,
        requests: 0,
      };
      entry.promptTokens += row.promptTokens;
      entry.completionTokens += row.completionTokens;
      entry.totalTokens += row.totalTokens;
      entry.requests += row.requests;
      byModel.set(row.model, entry);
    }
  }

  return {
    promptTokens,
    completionTokens,
    totalTokens: promptTokens + completionTokens,
    requests,
    models: [...byModel.values()].sort((a, b) => b.totalTokens - a.totalTokens),
  };
}

/** Share of a total as a percentage, guarding the empty case. */
export function shareOf(value: number, total: number): number {
  return total > 0 ? (value / total) * 100 : 0;
}
