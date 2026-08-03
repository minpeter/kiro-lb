import { describe, expect, it } from "vitest";
import { PANEL_UPPER_MIN_HEIGHT } from "./panel-metrics";
import { SLICE_LIMIT } from "./token-slices";

/**
 * The shared floor exists so the two Overview panels' dividers land on the same
 * line. It is a Tailwind class, so a unit test cannot measure the rendered
 * result - that was verified by driving the live dashboard. What a test can pin
 * is the reasoning behind the number, which is what would silently rot: the
 * floor has to clear a full-length legend, and it must apply only when the
 * panels actually share a row.
 */
const LINE_HEIGHT_PX = 20; // text-sm
const ROW_GAP_PX = 6; // space-y-1.5
const REM_PX = 4; // Tailwind's spacing unit

describe("PANEL_UPPER_MIN_HEIGHT", () => {
  it("only applies from xl, where the panels share a row", () => {
    // Stacked, each panel is alone and a forced height is just dead space.
    expect(PANEL_UPPER_MIN_HEIGHT.startsWith("xl:")).toBe(true);
  });

  it("is a min-height, so a taller legend is never clipped", () => {
    expect(PANEL_UPPER_MIN_HEIGHT).toMatch(/^xl:min-h-\d+$/);
  });

  it("clears a legend at full length", () => {
    const rows = SLICE_LIMIT + 1; // named models plus the grouped tail
    const legendHeight = rows * LINE_HEIGHT_PX + (rows - 1) * ROW_GAP_PX;

    const floorPx = Number(PANEL_UPPER_MIN_HEIGHT.replace("xl:min-h-", "")) * REM_PX;

    // Raising SLICE_LIMIT without raising the floor would crop the last row.
    expect(floorPx).toBeGreaterThanOrEqual(legendHeight);
  });

  it("stays on the 4px spacing scale", () => {
    const step = Number(PANEL_UPPER_MIN_HEIGHT.replace("xl:min-h-", ""));

    expect(Number.isInteger(step)).toBe(true);
  });
});
