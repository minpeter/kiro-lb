import { describe, expect, it } from "vitest";
import { buildSlices, shareLabel, TAIL_LABEL } from "./token-slices";
import type { ModelTotal } from "./format";

function model(name: string, totalTokens: number): ModelTotal {
  return { model: name, promptTokens: totalTokens, completionTokens: 0, totalTokens, requests: 1 };
}

/** Descending, as summarizeUsage returns them. */
function ranked(...totals: number[]): ModelTotal[] {
  return totals.map((value, index) => model(`model-${index}`, value));
}

describe("buildSlices", () => {
  it("gives each model its own slice when there are few", () => {
    const models = [model("a", 60), model("b", 40)];

    const slices = buildSlices(models, 100);

    expect(slices.map((s) => s.label)).toEqual(["a", "b"]);
    expect(slices.map((s) => s.share)).toEqual([60, 40]);
  });

  it("groups everything past the sixth model", () => {
    const slices = buildSlices(ranked(70, 6, 5, 4, 3, 2, 1, 1, 1), 93);

    expect(slices).toHaveLength(7);
    expect(slices[6].label).toBe(TAIL_LABEL);
    expect(slices[6].models).toBe(3);
    expect(slices[6].tokens).toBe(3);
  });

  it("keeps runners-up visible under extreme skew", () => {
    // Live data has one model at 97.8% and twenty others sharing 2.2%. Gating
    // slices on a minimum share collapsed all twenty into the tail and left a
    // two-slice chart, so selection is by rank instead.
    const dominant = 1_119_805_435;
    const others = [3_879_652, 2_523_931, 2_101_694, 2_060_821, 2_055_163, 1_752_886, 1_483_862];
    const total = dominant + others.reduce((sum, v) => sum + v, 0);

    const slices = buildSlices([model("claude-opus-5", dominant), ...others.map((v, i) => model(`m${i}`, v))], total);

    expect(slices[0].label).toBe("claude-opus-5");
    expect(slices[0].share).toBeGreaterThan(97);
    // Five named runners-up survive rather than being swallowed by the tail.
    expect(slices.slice(1, 6).every((s) => s.label !== TAIL_LABEL)).toBe(true);
    expect(slices.at(-1)?.label).toBe(TAIL_LABEL);
  });

  it("adds no tail slice when nothing is left over", () => {
    const slices = buildSlices(ranked(5, 4, 3), 12);

    expect(slices.some((s) => s.label === TAIL_LABEL)).toBe(false);
  });

  it("omits models with no tokens so they cannot draw a zero-width arc", () => {
    const slices = buildSlices([model("a", 10), model("b", 0)], 10);

    expect(slices.map((s) => s.label)).toEqual(["a"]);
  });

  it("keeps the slices summing to the total", () => {
    const models = ranked(1000, 500, 250, 125, 60, 30, 15, 7, 3, 1);
    const total = models.reduce((sum, m) => sum + m.totalTokens, 0);

    const slices = buildSlices(models, total);

    // The donut converts each share into an arc length; if these drift the ring
    // either overlaps itself or leaves a gap.
    expect(slices.reduce((sum, s) => sum + s.tokens, 0)).toBe(total);
    expect(slices.reduce((sum, s) => sum + s.share, 0)).toBeCloseTo(100, 6);
  });

  it("assigns the tail a distinct colour from every named model", () => {
    const slices = buildSlices(ranked(10, 9, 8, 7, 6, 5, 4, 3), 52);
    const tail = slices.at(-1);

    expect(tail?.label).toBe(TAIL_LABEL);
    expect(slices.slice(0, -1).some((s) => s.color === tail?.color)).toBe(false);
  });

  it("returns nothing for an empty model list", () => {
    expect(buildSlices([], 0)).toEqual([]);
  });
});

describe("shareLabel", () => {
  it("rounds a substantial share to a whole percent", () => {
    expect(shareLabel(97.8, 1_000)).toBe("98%");
  });

  it("keeps a decimal for a sub-1% share", () => {
    // Output tokens were 3.7M against 1.1B input and rendered as "0%", which
    // read as "no output tokens at all".
    expect(shareLabel(0.32, 3_700_000)).toBe("0.3%");
  });

  it("marks a vanishingly small but real share", () => {
    expect(shareLabel(0.004, 5_000)).toBe("<0.1%");
  });

  it("shows a true zero as 0%", () => {
    expect(shareLabel(0, 0)).toBe("0%");
  });
});
