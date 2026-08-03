/**
 * Shared vertical rhythm for the two Overview panels.
 *
 * `Total request rate` and `Token usage by model` sit side by side from `xl` up,
 * and each draws a divider above its summary figures. Those dividers only line up
 * if the region above them is the same height in both cards, which it is not by
 * nature: the rate chart is 144px of plot, the token panel is 176px of donut and
 * legend.
 *
 * The taller of the two sets the floor. 176px is the legend at full length -
 * SLICE_LIMIT (6) named models plus the grouped tail, at `text-sm`'s 20px line
 * height with `space-y-1.5` (6px) between rows - so pinning to it never clips the
 * legend, only pads the shorter chart.
 *
 * Tailwind class rather than a style prop so the value stays in the design
 * system's 4px scale: min-h-44 is 11rem is 176px. It is applied only from `xl`,
 * because when the panels stack there is no neighbour to align with and forcing
 * the height would just add dead space on a phone.
 */
export const PANEL_UPPER_MIN_HEIGHT = "xl:min-h-44";
