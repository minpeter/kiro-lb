import { shareOf, type ModelTotal } from "./format";

/**
 * How many models get their own slice; the rest are grouped.
 *
 * Selection is by rank, not by share. Real traffic here is extremely top-heavy -
 * one model holds 97.8% of all tokens - so a minimum-share gate collapsed every
 * other model into the tail and left a two-slice chart. The runners-up draw arcs
 * barely a pixel wide, but they are named with exact figures in the legend, which
 * is what makes the panel readable under this much skew.
 */
export const SLICE_LIMIT = 6;

// Five theme chart colors, then a muted blend for the sixth. The tail always
// takes the least prominent colour so it does not read as a real model.
const SLICE_COLORS = [
  "var(--chart-1)",
  "var(--chart-2)",
  "var(--chart-3)",
  "var(--chart-4)",
  "var(--chart-5)",
  "color-mix(in oklch, var(--chart-2) 55%, var(--muted))",
];
const TAIL_COLOR = "var(--muted-foreground)";

export const TAIL_LABEL = "other models";

export type Slice = {
  label: string;
  tokens: number;
  share: number;
  color: string;
  /** How many models this slice stands for; >1 only for the tail. */
  models: number;
};

/** A nonzero share must not round to "0%": output is often <0.5% of input. */
export function shareLabel(share: number, value: number): string {
  if (value === 0) return "0%";
  if (share < 0.1) return "<0.1%";
  if (share < 1) return `${share.toFixed(1)}%`;
  return `${Math.round(share)}%`;
}

/** Rank models into at most SLICE_LIMIT named slices plus a grouped tail. */
export function buildSlices(models: ModelTotal[], total: number): Slice[] {
  // A zero-token model would draw a zero-width arc and add a legend row that
  // says nothing.
  const ranked = models.filter((model) => model.totalTokens > 0);
  const head = ranked.slice(0, SLICE_LIMIT);
  const tail = ranked.slice(SLICE_LIMIT);

  const slices: Slice[] = head.map((model, index) => ({
    label: model.model,
    tokens: model.totalTokens,
    share: shareOf(model.totalTokens, total),
    color: SLICE_COLORS[index % SLICE_COLORS.length],
    models: 1,
  }));

  const tailTokens = tail.reduce((sum, model) => sum + model.totalTokens, 0);
  if (tailTokens > 0) {
    slices.push({
      label: TAIL_LABEL,
      tokens: tailTokens,
      share: shareOf(tailTokens, total),
      color: TAIL_COLOR,
      models: tail.length,
    });
  }
  return slices;
}
