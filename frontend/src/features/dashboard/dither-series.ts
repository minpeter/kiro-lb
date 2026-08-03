import type { DitherColor } from "@/components/dither-kit/palette";
import type { RateTotals } from "./request-rate-totals";
import type { Slice } from "./token-slices";
import type { AccountRateSeries } from "./types";

/**
 * Adapters from the dashboard's own aggregates to dither-kit's `{data, config}`
 * pair. They live outside the panels because `react-refresh/only-export-components`
 * forbids exporting them from a `.tsx`, and because the mapping is the part worth
 * unit-testing: the charts themselves paint on a canvas nothing can assert on.
 */

export type DitherConfigEntry = { label?: string; color: DitherColor };
export type DitherConfig = Record<string, DitherConfigEntry>;

/**
 * Slice colour order. dither-kit takes named hues rather than CSS variables, so
 * the theme's `--chart-N` ramp cannot be reused; this is the same ordering
 * intent - distinct neighbours, warm accents last - in the kit's palette.
 */
export const ditherPalette: DitherColor[] = ["blue", "purple", "green", "orange", "pink", "red"];

/** The grouped tail is grey, matching the kit's own "nothing here" hue. */
const TAIL_COLOR: DitherColor = "grey";

/** Traffic at or above this share of the observed limit is drawn as a warning. */
const WARN_FRACTION = 0.8;

function formatClock(unixSeconds: number): string {
  return new Date(unixSeconds * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export type RateRow = { at: string; served: number; rejected: number };

export function rateChartRows(totals: RateTotals): RateRow[] {
  return totals.perMinute.map((served, index) => ({
    at: formatClock(totals.bucketStarts[index] ?? 0),
    served,
    rejected: totals.rateLimitedPerMinute[index] ?? 0,
  }));
}

/**
 * The rejected series is configured only when something was actually rejected: a
 * flat zero line and a legend entry for it would imply the gateway is shedding
 * traffic when it is not.
 */
export function rateChartConfig(totals: RateTotals): DitherConfig {
  const config: DitherConfig = { served: { label: "served", color: "blue" } };
  if (totals.rateLimited > 0) config.rejected = { label: "rejected", color: "red" };
  return config;
}

export type AccountRateRow = { rpm: number };

/** The guide sits above traffic with room to spare, so the scale anchors here. */
const LIMIT_HEADROOM = 1.25;

export type AccountRateView = {
  rows: AccountRateRow[];
  config: DitherConfig;
  /** Forced top of the y-domain, so an off-scale limit guide stays in the plot. */
  yMax?: number;
  peak: number;
  hasTraffic: boolean;
  /** Peak as a fraction of the observed limit, or null when none is known. */
  load: number | null;
  nearLimit: boolean;
  color: DitherColor;
  rejected: number;
};

export function accountRateSeries(series: AccountRateSeries): AccountRateView {
  const peak = Math.max(...series.peakRpm, 0);
  const load = series.limitRpm === null || series.limitRpm === 0 ? null : peak / series.limitRpm;
  const nearLimit = load !== null && load >= WARN_FRACTION;

  return {
    rows: series.peakRpm.map((rpm) => ({ rpm })),
    config: { rpm: { label: "req/min", color: nearLimit ? "red" : "blue" } },
    // The engine derives its y-domain from the data, so a limit above observed
    // traffic would be drawn outside the plot box without this floor.
    yMax: series.limitRpm === null ? undefined : series.limitRpm * LIMIT_HEADROOM,
    peak,
    hasTraffic: series.peakRpm.some(Boolean),
    load,
    nearLimit,
    color: nearLimit ? "red" : "blue",
    rejected: series.rateLimited.reduce((sum, value) => sum + value, 0),
  };
}

export type PieRow = { slice: string; tokens: number };

/**
 * Slices are keyed positionally (`s0`, `s1`, …) rather than by model name: the
 * name is client-controlled and can contain any character, while dither-kit uses
 * the key to look colours up in `config`.
 */
function sliceId(index: number): string {
  return `s${index}`;
}

export function tokenPieRows(slices: Slice[]): PieRow[] {
  return slices
    .map((slice, index) => ({ slice: sliceId(index), tokens: slice.tokens }))
    .filter((row) => row.tokens > 0);
}

export function tokenPieConfig(slices: Slice[], tailLabel = "other models"): DitherConfig {
  const config: DitherConfig = {};
  slices.forEach((slice, index) => {
    config[sliceId(index)] = {
      label: slice.label,
      color: slice.label === tailLabel ? TAIL_COLOR : ditherPalette[index % ditherPalette.length],
    };
  });
  return config;
}
