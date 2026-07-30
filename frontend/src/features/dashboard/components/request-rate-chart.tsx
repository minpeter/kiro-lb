import { Activity } from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/empty-state";
import type { AccountRateSeries, RequestRate } from "../types";
import { ChartSkeleton } from "./skeletons";

const CHART_HEIGHT = 96;
const OUTCOME_STYLE = {
  success: { fill: "var(--chart-1)", label: "served" },
  rateLimited: { fill: "var(--destructive)", label: "429 rate limited" },
  failure: { fill: "var(--muted-foreground)", label: "other failure" },
} as const;

function bucketTotal(series: AccountRateSeries, index: number): number {
  return series.success[index] + series.rateLimited[index] + series.failure[index];
}

function formatClock(unixSeconds: number): string {
  return new Date(unixSeconds * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function perMinute(count: number, bucketSeconds: number): string {
  return ((count * 60) / bucketSeconds).toFixed(1);
}

function AccountRateRow({ series, rate, peak }: { series: AccountRateSeries; rate: RequestRate; peak: number }) {
  const buckets = rate.bucketStarts.length;
  const total = rate.bucketStarts.reduce((sum, _, index) => sum + bucketTotal(series, index), 0);
  const rateLimited = series.rateLimited.reduce((sum, value) => sum + value, 0);
  const busiest = Math.max(...rate.bucketStarts.map((_, index) => bucketTotal(series, index)), 0);

  return (
    <div className="space-y-1.5">
      <div className="flex items-baseline justify-between gap-2 text-xs">
        <span className="font-mono">{series.account}</span>
        <span className="tabular-nums text-muted-foreground">
          {total} req · peak {perMinute(busiest, rate.bucketSeconds)}/min
          {rateLimited > 0 && <span className="text-destructive"> · {rateLimited} rate limited</span>}
        </span>
      </div>
      <svg
        viewBox={`0 0 ${buckets} ${CHART_HEIGHT}`}
        preserveAspectRatio="none"
        className="h-24 w-full overflow-visible"
        role="img"
        aria-label={`Request rate for account ${series.account}: ${total} requests, ${rateLimited} rate limited`}
      >
        {rate.bucketStarts.map((start, index) => {
          const counts = [
            ["failure", series.failure[index]],
            ["rateLimited", series.rateLimited[index]],
            ["success", series.success[index]],
          ] as const;
          let offset = 0;
          return (
            <g key={start}>
              {counts.map(([outcome, count]) => {
                if (count === 0) return null;
                const height = (count / peak) * CHART_HEIGHT;
                const y = CHART_HEIGHT - offset - height;
                offset += height;
                return (
                  <rect
                    key={outcome}
                    x={index + 0.15}
                    y={y}
                    width={0.7}
                    height={height}
                    fill={OUTCOME_STYLE[outcome].fill}
                  />
                );
              })}
            </g>
          );
        })}
      </svg>
    </div>
  );
}

export function RequestRateChart({ rate, isLoading }: { rate?: RequestRate; isLoading: boolean }) {
  const peak = rate
    ? Math.max(
        1,
        ...rate.accounts.flatMap((series) => rate.bucketStarts.map((_, index) => bucketTotal(series, index))),
      )
    : 1;
  const hasTraffic = rate?.accounts.some((series) => series.success.some(Boolean) || series.rateLimited.some(Boolean) || series.failure.some(Boolean));

  return (
    <Card>
      <CardHeader>
        <CardTitle>Per-account request rate</CardTitle>
        <CardDescription>
          {rate
            ? `${rate.bucketSeconds}s buckets, ${formatClock(rate.bucketStarts[0])}–${formatClock(
                rate.bucketStarts[rate.bucketStarts.length - 1] + rate.bucketSeconds,
              )}. Upstream verdicts, so 429s recovered by failover are visible here but not in the request log.`
            : "Upstream routing outcomes per account."}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-5">
        {isLoading || !rate ? (
          <ChartSkeleton />
        ) : !hasTraffic ? (
          <EmptyState icon={Activity} title="No traffic in this window" description="Routing outcomes appear here as requests arrive." />
        ) : (
          <>
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
              {Object.entries(OUTCOME_STYLE).map(([outcome, style]) => (
                <span key={outcome} className="flex items-center gap-1.5">
                  <span aria-hidden className="size-2 rounded-sm" style={{ background: style.fill }} />
                  {style.label}
                </span>
              ))}
            </div>
            {rate.accounts.map((series) => (
              <AccountRateRow key={series.account} series={series} rate={rate} peak={peak} />
            ))}
          </>
        )}
      </CardContent>
    </Card>
  );
}
