import { useMemo, useState } from "react";
import { Coins } from "lucide-react";
import { Pie } from "@/components/dither-kit/pie";
import { PieChart } from "@/components/dither-kit/pie-chart";
import { Tooltip } from "@/components/dither-kit/tooltip";
import { rgb, seedOfColor } from "@/components/dither-kit/palette";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/empty-state";
import { ChartSkeleton } from "./skeletons";
import { exactTokens, formatTokens, shareOf, summarizeUsage } from "../format";
import { sliceIdAt, tokenPieConfig, tokenPieRows, type DitherConfig } from "../dither-series";
import { buildSlices, shareLabel, TAIL_LABEL, type Slice } from "../token-slices";
import { PANEL_UPPER_MIN_HEIGHT } from "../panel-metrics";
import type { KeyUsage } from "../types";

/** Donut hole as a fraction of the outer radius, leaving room for the total. */
const INNER_RADIUS = 0.62;

function Donut({
  rows,
  config,
  total,
  slices,
  focused,
  onFocus,
}: {
  rows: { slice: string; tokens: number }[];
  config: DitherConfig;
  total: number;
  slices: Slice[];
  focused: string | null;
  onFocus: (sliceId: string | null) => void;
}) {
  return (
    <div
      className="relative size-44 shrink-0"
      role="img"
      aria-label={`Token share by model. ${slices
        .map((slice) => `${slice.label}: ${slice.share.toFixed(1)}%`)
        .join(", ")}.`}
    >
      <PieChart
        data={rows}
        config={config}
        dataKey="tokens"
        nameKey="slice"
        innerRadius={INNER_RADIUS}
        bloom="low"
        bloomOnHover
        animate={false}
        margins={{ top: 4, right: 4, bottom: 4, left: 4 }}
        // Hovering a wedge spotlights it and lifts its legend row. The engine
        // reports the wedge index, which is not the slice position once zero or
        // sub-1% slices are dropped, so the key is read back from the rows.
        focusDataKey={focused}
        onHoverChange={(index) => onFocus(sliceIdAt(rows, index))}
      >
        <Pie variant="gradient" />
        <Tooltip valueFormatter={(value) => formatTokens(value)} />
      </PieChart>
      {/* Pointer-events off so the ring underneath keeps its hover slices. */}
      <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
        <span className="text-xl font-semibold tabular-nums" title={exactTokens(total)}>
          {formatTokens(total)}
        </span>
        <span className="text-[11px] text-muted-foreground">total tokens</span>
      </div>
    </div>
  );
}

function Legend({
  slices,
  config,
  focused,
  onFocus,
}: {
  slices: Slice[];
  config: DitherConfig;
  focused: string | null;
  onFocus: (sliceId: string | null) => void;
}) {
  return (
    <ul className="min-w-0 flex-1 space-y-1.5">
      {slices.map((slice, index) => {
        const sliceId = `s${index}`;
        const dimmed = focused !== null && focused !== sliceId;
        return (
          <li key={slice.label}>
            <button
              type="button"
              // Pointer and keyboard focus spotlight the matching wedge. Tiny
              // legend-only slices still receive the same accessible affordance.
              onPointerEnter={() => onFocus(sliceId)}
              onPointerLeave={() => onFocus(null)}
              onFocus={() => onFocus(sliceId)}
              onBlur={() => onFocus(null)}
              className={`flex w-full items-center gap-2 rounded-sm text-left text-sm transition-opacity focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                dimmed ? "opacity-40" : ""
              } ${focused === sliceId ? "bg-muted/50" : ""}`}
            >
              <span
                aria-hidden
                className="size-2.5 shrink-0 rounded-sm"
                // The swatch reads the same dither seed the wedge is painted with, so
                // the legend cannot drift from the ring.
                style={{ backgroundColor: rgb(seedOfColor(config[sliceId].color).fill) }}
              />
              <span
                className={`truncate font-mono text-xs ${slice.label === TAIL_LABEL ? "text-muted-foreground" : ""}`}
              >
                {slice.label}
                {slice.models > 1 && <span className="text-muted-foreground"> ({slice.models})</span>}
              </span>
              <span className="ml-auto shrink-0 tabular-nums" title={exactTokens(slice.tokens)}>
                {formatTokens(slice.tokens)}
              </span>
              <span className="w-12 shrink-0 text-right text-xs tabular-nums text-muted-foreground">
                {shareLabel(slice.share, slice.tokens)}
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}

export function TokenUsagePanel({ keyUsage, isLoading }: { keyUsage: KeyUsage; isLoading: boolean }) {
  const totals = useMemo(() => summarizeUsage(keyUsage), [keyUsage]);
  const slices = useMemo(
    () => buildSlices(totals.models, totals.totalTokens),
    [totals.models, totals.totalTokens],
  );
  const rows = useMemo(
    () =>
      tokenPieRows(slices).filter((row) => {
        const sliceIndex = Number(row.slice.slice(1));
        return (slices[sliceIndex]?.share ?? 0) >= 1;
      }),
    [slices],
  );
  const config = useMemo(() => tokenPieConfig(slices, TAIL_LABEL), [slices]);
  // Shared hover so the ring and the legend spotlight the same slice, whichever
  // one the pointer is over.
  const [focused, setFocused] = useState<string | null>(null);

  const inputShare = shareOf(totals.promptTokens, totals.totalTokens);

  return (
    <Card className="@container/panel flex flex-col">
      <CardHeader>
        <CardTitle>Token usage by model</CardTitle>
        <CardDescription>
          Cumulative across every API key, including revoked ones: a revoked key&apos;s history still counts toward what
          this gateway has consumed.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex-1">
        {isLoading ? (
          <ChartSkeleton rows={1} />
        ) : totals.totalTokens === 0 ? (
          <EmptyState
            icon={Coins}
            title="No tokens recorded yet"
            description="Usage appears once a request completes through /v1."
          />
        ) : (
          <div className="space-y-4">
            {/* The donut and legend sit side by side once the card is wide enough,
                and stack below that. Keyed to the container, not the viewport,
                because this panel is half-width on a large screen. */}
            {/* Same floor as the rate chart's plot area, so both dividers land on
                the same line when the panels share a row. A short legend pads;
                a full one already fills this height. */}
            <div
              className={`flex flex-col items-center justify-center gap-6 @sm/panel:flex-row @sm/panel:items-center ${PANEL_UPPER_MIN_HEIGHT}`}
            >
              <Donut
                rows={rows}
                config={config}
                total={totals.totalTokens}
                slices={slices}
                focused={focused}
                onFocus={setFocused}
              />
              <Legend slices={slices} config={config} focused={focused} onFocus={setFocused} />
            </div>

            <dl className="grid grid-cols-2 gap-3 border-t pt-4 @2xl/panel:grid-cols-4">
              <div>
                <dt className="text-xs text-muted-foreground">Input</dt>
                <dd className="tabular-nums" title={exactTokens(totals.promptTokens)}>
                  {formatTokens(totals.promptTokens)}
                  <span className="ml-1 text-xs text-muted-foreground">
                    {shareLabel(inputShare, totals.promptTokens)}
                  </span>
                </dd>
              </div>
              <div>
                <dt className="text-xs text-muted-foreground">Output</dt>
                <dd className="tabular-nums" title={exactTokens(totals.completionTokens)}>
                  {formatTokens(totals.completionTokens)}
                  <span className="ml-1 text-xs text-muted-foreground">
                    {shareLabel(100 - inputShare, totals.completionTokens)}
                  </span>
                </dd>
              </div>
              <div>
                <dt className="text-xs text-muted-foreground">Requests</dt>
                <dd className="tabular-nums">{totals.requests.toLocaleString()}</dd>
              </div>
              <div>
                <dt className="text-xs text-muted-foreground">Models used</dt>
                <dd className="tabular-nums">{totals.models.length}</dd>
              </div>
            </dl>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
