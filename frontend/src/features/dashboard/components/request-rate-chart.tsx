import { Activity } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/empty-state";
import type { AccountRateSeries, RequestRate } from "../types";
import { ChartSkeleton } from "./skeletons";

const CHART_HEIGHT = 100;
const SERVED_FILL = "var(--chart-1)";
const LIMIT_STROKE = "var(--destructive)";

function formatClock(unixSeconds: number): string {
  return new Date(unixSeconds * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function scaleFor(series: AccountRateSeries): number {
  // Include the ceiling so headroom stays visible when traffic is far below it.
  const observed = Math.max(...series.peakRpm, series.ceilingRpm ?? 0, 1);
  return observed * 1.15;
}

function AccountRatePanel({ series, rate }: { series: AccountRateSeries; rate: RequestRate }) {
  const buckets = rate.bucketStarts.length;
  const scale = scaleFor(series);
  const peak = Math.max(...series.peakRpm, 0);
  const rateLimited = series.rateLimited.reduce((sum, value) => sum + value, 0);
  const ceilingY = series.ceilingRpm === null ? null : CHART_HEIGHT - (series.ceilingRpm / scale) * CHART_HEIGHT;

  const area = [
    `M 0 ${CHART_HEIGHT}`,
    ...series.peakRpm.map((value, index) => `L ${index} ${CHART_HEIGHT - (value / scale) * CHART_HEIGHT}`),
    `L ${buckets - 1} ${CHART_HEIGHT}`,
    "Z",
  ].join(" ");
  const line = series.peakRpm
    .map((value, index) => `${index === 0 ? "M" : "L"} ${index} ${CHART_HEIGHT - (value / scale) * CHART_HEIGHT}`)
    .join(" ");

  return (
    <div className="space-y-2 rounded-lg border p-3">
      <div className="flex items-baseline justify-between gap-2">
        <span className="font-mono text-xs">{series.account}</span>
        <span className="text-xs tabular-nums text-muted-foreground">
          peak {peak}/min
          {rateLimited > 0 && <span className="text-destructive"> · {rateLimited} rate limited</span>}
        </span>
      </div>

      <div className="relative">
        <svg
          viewBox={`0 0 ${Math.max(buckets - 1, 1)} ${CHART_HEIGHT}`}
          preserveAspectRatio="none"
          className="h-28 w-full"
          role="img"
          aria-label={`Peak requests per minute for account ${series.account}, peak ${peak}, observed limit ${
            series.ceilingRpm ?? "unknown"
          }`}
        >
          <path d={area} fill={SERVED_FILL} opacity={0.28} />
          <path d={line} fill="none" stroke={SERVED_FILL} strokeWidth={1} vectorEffect="non-scaling-stroke" />
          {ceilingY !== null && (
            <line
              x1={0}
              x2={Math.max(buckets - 1, 1)}
              y1={ceilingY}
              y2={ceilingY}
              stroke={LIMIT_STROKE}
              strokeWidth={1}
              strokeDasharray="4 3"
              vectorEffect="non-scaling-stroke"
            />
          )}
        </svg>
        {series.ceilingRpm !== null && (
          <Badge variant="destructive" className="absolute left-0 -translate-y-1/2 text-[10px]" style={{ top: `${(ceilingY! / CHART_HEIGHT) * 100}%` }}>
            ~{series.ceilingRpm}/min
          </Badge>
        )}
        <span className="pointer-events-none absolute right-0 top-0 text-[10px] tabular-nums text-muted-foreground">
          {Math.round(scale)}
        </span>
      </div>

      <p className="text-xs text-muted-foreground">
        {series.ceilingRpm === null ? (
          "No rate rejection observed yet, so no ceiling can be estimated."
        ) : (
          <>
            Limit is between {series.servedPeakRpm}/min served and ~{series.ceilingRpm}/min rejected
            {series.rateLimitSamples === 1 ? " (1 sample, rough)" : ` (${series.rateLimitSamples} samples)`}
          </>
        )}
      </p>
    </div>
  );
}

export function RequestRateChart({ rate, isLoading }: { rate?: RequestRate; isLoading: boolean }) {
  const hasTraffic = rate?.accounts.some((series) => series.peakRpm.some(Boolean));

  return (
    <Card>
      <CardHeader>
        <CardTitle>Per-account request rate</CardTitle>
        <CardDescription>
          {rate
            ? `Peak requests per minute in ${rate.bucketSeconds}s buckets, ${formatClock(rate.bucketStarts[0])}–${formatClock(
                rate.bucketStarts[rate.bucketStarts.length - 1] + rate.bucketSeconds,
              )}. Kiro publishes no rate limit, so the dashed line is inferred from observed 429s, not an official figure.`
            : "Peak requests per minute per account."}
        </CardDescription>
      </CardHeader>
      <CardContent>
        {isLoading || !rate ? (
          <ChartSkeleton />
        ) : !hasTraffic ? (
          <EmptyState icon={Activity} title="No traffic in this window" description="Routing outcomes appear here as requests arrive." />
        ) : (
          <div className="grid gap-3 lg:grid-cols-2">
            {rate.accounts.map((series) => (
              <AccountRatePanel key={series.account} series={series} rate={rate} />
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
