import { useMemo } from "react";
import { Coins } from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/empty-state";
import { ChartSkeleton } from "./skeletons";
import { exactTokens, formatTokens, shareOf, summarizeUsage } from "../format";
import { buildSlices, shareLabel, TAIL_LABEL, type Slice } from "../token-slices";
import type { KeyUsage } from "../types";

// Donut geometry. The ring is drawn as one circle per slice, so every segment
// shares these dimensions.
const RADIUS = 60;
const STROKE = 26;
const CIRCUMFERENCE = 2 * Math.PI * RADIUS;

function Donut({ slices, total }: { slices: Slice[]; total: number }) {
  // Drawn with stroke-dasharray on concentric circles rather than arc paths:
  // one segment per slice, no trigonometry, and no seams between segments.
  // Offsets are the running sum of preceding lengths, computed without mutating
  // anything during render.
  const segments = slices.map((slice, index) => {
    const length = (slice.tokens / total) * CIRCUMFERENCE;
    const offset = slices
      .slice(0, index)
      .reduce((sum, previous) => sum + (previous.tokens / total) * CIRCUMFERENCE, 0);
    return { ...slice, length, offset };
  });

  return (
    <div className="relative shrink-0">
      <svg
        viewBox="0 0 160 160"
        className="h-40 w-40 -rotate-90"
        role="img"
        aria-label={`Token share by model. ${slices
          .map((slice) => `${slice.label}: ${slice.share.toFixed(1)}%`)
          .join(", ")}.`}
      >
        <circle cx={80} cy={80} r={RADIUS} fill="none" stroke="var(--muted)" strokeWidth={STROKE} opacity={0.35} />
        {segments.map((segment) => (
          <circle
            key={segment.label}
            cx={80}
            cy={80}
            r={RADIUS}
            fill="none"
            stroke={segment.color}
            strokeWidth={STROKE}
            strokeDasharray={`${segment.length} ${CIRCUMFERENCE - segment.length}`}
            strokeDashoffset={-segment.offset}
          >
            <title>{`${segment.label}: ${formatTokens(segment.tokens)} (${segment.share.toFixed(1)}%)`}</title>
          </circle>
        ))}
      </svg>
      <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
        <span className="text-xl font-semibold tabular-nums" title={exactTokens(total)}>
          {formatTokens(total)}
        </span>
        <span className="text-[11px] text-muted-foreground">total tokens</span>
      </div>
    </div>
  );
}

function Legend({ slices }: { slices: Slice[] }) {
  return (
    <ul className="min-w-0 flex-1 space-y-1.5">
      {slices.map((slice) => (
        <li key={slice.label} className="flex items-center gap-2 text-sm">
          <span aria-hidden className="size-2.5 shrink-0 rounded-sm" style={{ backgroundColor: slice.color }} />
          <span className={`truncate font-mono text-xs ${slice.label === TAIL_LABEL ? "text-muted-foreground" : ""}`}>
            {slice.label}
            {slice.models > 1 && <span className="text-muted-foreground"> ({slice.models})</span>}
          </span>
          <span className="ml-auto shrink-0 tabular-nums" title={exactTokens(slice.tokens)}>
            {formatTokens(slice.tokens)}
          </span>
          <span className="w-12 shrink-0 text-right text-xs tabular-nums text-muted-foreground">
            {shareLabel(slice.share, slice.tokens)}
          </span>
        </li>
      ))}
    </ul>
  );
}

export function TokenUsagePanel({ keyUsage, isLoading }: { keyUsage: KeyUsage; isLoading: boolean }) {
  const totals = useMemo(() => summarizeUsage(keyUsage), [keyUsage]);
  const slices = useMemo(
    () => buildSlices(totals.models, totals.totalTokens),
    [totals.models, totals.totalTokens],
  );

  const inputShare = shareOf(totals.promptTokens, totals.totalTokens);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Token usage by model</CardTitle>
        <CardDescription>
          Cumulative across every API key, including revoked ones: a revoked key&apos;s history still counts toward what
          this gateway has consumed.
        </CardDescription>
      </CardHeader>
      <CardContent>
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
            <div className="flex flex-wrap items-center gap-6">
              <Donut slices={slices} total={totals.totalTokens} />
              <Legend slices={slices} />
            </div>

            <dl className="grid grid-cols-2 gap-3 border-t pt-4 sm:grid-cols-4">
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
