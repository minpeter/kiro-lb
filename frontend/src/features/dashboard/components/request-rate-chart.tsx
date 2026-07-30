import { Activity } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/empty-state";
import type { AccountRateSeries, RequestRate } from "../types";
import { ChartSkeleton } from "./skeletons";

const CHART_HEIGHT = 100;
const LIMIT_STROKE = "var(--destructive)";
const WARN_FRACTION = 0.8;

function formatClock(unixSeconds: number): string {
  return new Date(unixSeconds * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function loadFactor(series: AccountRateSeries): number | null {
  if (series.limitRpm === null || series.limitRpm === 0) return null;
  return Math.max(...series.peakRpm, 0) / series.limitRpm;
}

function AccountRatePanel({ series, rate }: { series: AccountRateSeries; rate: RequestRate }) {
  const buckets = Math.max(rate.bucketStarts.length - 1, 1);
  const peak = Math.max(...series.peakRpm, 0);
  const load = loadFactor(series);
  const nearLimit = load !== null && load >= WARN_FRACTION;
  const rateLimited = series.rateLimited.reduce((sum, value) => sum + value, 0);

  // The guide must sit above traffic with room to spare, so it anchors the
  // scale at 125%: traffic touching the line is then visibly the warning.
  const scale = series.limitRpm !== null ? series.limitRpm * 1.25 : Math.max(peak, 1) * 1.15;
  const y = (value: number) => CHART_HEIGHT - (value / scale) * CHART_HEIGHT;
  const limitY = series.limitRpm === null ? null : y(series.limitRpm);

  const area = [
    `M 0 ${CHART_HEIGHT}`,
    ...series.peakRpm.map((value, index) => `L ${index} ${y(value)}`),
    `L ${buckets} ${CHART_HEIGHT}`,
    "Z",
  ].join(" ");
  const line = series.peakRpm.map((value, index) => `${index === 0 ? "M" : "L"} ${index} ${y(value)}`).join(" ");
  const traffic = nearLimit ? LIMIT_STROKE : "var(--chart-1)";

  return (
    <div className="space-y-2 rounded-lg border p-3">
      <div className="flex items-baseline justify-between gap-2">
        <span className="font-mono text-xs">{series.account}</span>
        <span className="text-xs tabular-nums text-muted-foreground">
          {peak}/min peak
          {load !== null && (
            <span className={nearLimit ? "text-destructive" : undefined}> · {Math.round(load * 100)}% of limit</span>
          )}
          {rateLimited > 0 && <span className="text-destructive"> · {rateLimited} rejected</span>}
        </span>
      </div>

      <div className="relative">
        <svg
          viewBox={`0 0 ${buckets} ${CHART_HEIGHT}`}
          preserveAspectRatio="none"
          className="h-28 w-full"
          role="img"
          aria-label={`Peak requests per minute for account ${series.account}: ${peak} per minute${
            series.limitRpm === null ? ", no observed limit" : `, observed limit ${series.limitRpm} per minute`
          }`}
        >
          {limitY !== null && (
            <rect x={0} y={0} width={buckets} height={limitY} fill={LIMIT_STROKE} opacity={0.06} />
          )}
          {/* Uncertainty band: the limit is somewhere between safe and rejected. */}
          {limitY !== null && series.safeRpm > 0 && (
            <rect
              x={0}
              y={limitY}
              width={buckets}
              height={Math.max(y(series.safeRpm) - limitY, 0)}
              fill={LIMIT_STROKE}
              opacity={0.08}
            />
          )}
          <path d={area} fill={traffic} opacity={0.28} />
          <path d={line} fill="none" stroke={traffic} strokeWidth={1} vectorEffect="non-scaling-stroke" />
          {limitY !== null && (
            <line
              x1={0}
              x2={buckets}
              y1={limitY}
              y2={limitY}
              stroke={LIMIT_STROKE}
              strokeWidth={1}
              strokeDasharray="4 3"
              vectorEffect="non-scaling-stroke"
            />
          )}
        </svg>
        {series.limitRpm !== null && limitY !== null && (
          <Badge
            variant="destructive"
            className="absolute left-0 -translate-y-1/2 text-[10px]"
            style={{ top: `${(limitY / CHART_HEIGHT) * 100}%` }}
          >
            ~{series.limitRpm}/min
          </Badge>
        )}
      </div>

      <p className="text-xs text-muted-foreground">
        {series.limitRpm === null ? (
          <>
            No guide yet: {series.limitUnknownReason}.
            {series.safeRpm > 0 && ` Served ${series.safeRpm}/min without rejection.`}
          </>
        ) : nearLimit ? (
          <span className="text-destructive">
            Approaching the observed limit. Rejections start near ~{series.limitRpm}/min.
          </span>
        ) : (
          <>
            Limit between {series.safeRpm} and {series.limitRpm}/min (±{series.limitPrecisionRpm}), from{" "}
            {series.informativeSamples} rejection{series.informativeSamples === 1 ? "" : "s"} in the last{" "
            }{Math.round(series.estimateWindowSeconds / 3600)}h.
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
              )}. The dashed guide marks where rejections have started; Kiro publishes no limit, so it is inferred from observed 429s.`
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
