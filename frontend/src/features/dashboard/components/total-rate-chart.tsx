import { useMemo } from "react";
import { Activity } from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/empty-state";
import { ChartSkeleton } from "./skeletons";
import { summarizeRate, throttledAccounts } from "../request-rate-totals";
import type { RequestRate } from "../types";

const CHART_HEIGHT = 100;

function formatClock(unixSeconds: number): string {
  return new Date(unixSeconds * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function round(value: number): string {
  // A rate is rarely a whole number once buckets are scaled to a minute, but a
  // trailing ".0" on every axis label is noise.
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}

function Figure({ label, value, hint, tone }: { label: string; value: string; hint?: string; tone?: "warning" }) {
  return (
    <div>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className={`tabular-nums ${tone === "warning" ? "text-destructive" : ""}`}>
        {value}
        {hint && <span className="ml-1 text-xs text-muted-foreground">{hint}</span>}
      </dd>
    </div>
  );
}

export function TotalRateChart({ rate, isLoading }: { rate?: RequestRate; isLoading: boolean }) {
  const totals = useMemo(() => summarizeRate(rate), [rate]);
  const throttled = useMemo(() => throttledAccounts(rate), [rate]);

  const hasTraffic = totals.requests > 0;
  // Anchor the scale slightly above the peak so the busiest bucket does not sit
  // flush against the top edge and read as clipped.
  const scale = Math.max(totals.peakPerMinute * 1.15, 1);
  const y = (value: number) => CHART_HEIGHT - (value / scale) * CHART_HEIGHT;
  const lastIndex = Math.max(totals.perMinute.length - 1, 1);

  const area = [
    `M 0 ${CHART_HEIGHT}`,
    ...totals.perMinute.map((value, index) => `L ${index} ${y(value)}`),
    `L ${lastIndex} ${CHART_HEIGHT}`,
    "Z",
  ].join(" ");
  const line = totals.perMinute.map((value, index) => `${index === 0 ? "M" : "L"} ${index} ${y(value)}`).join(" ");
  const meanY = y(totals.meanPerMinute);

  return (
    <Card className="@container/panel flex flex-col">
      <CardHeader>
        <CardTitle>Total request rate</CardTitle>
        <CardDescription>
          {rate
            ? `Every account combined, in ${rate.bucketSeconds}s buckets, ${formatClock(
                rate.bucketStarts[0],
              )}–${formatClock(rate.bucketStarts[rate.bucketStarts.length - 1] + rate.bucketSeconds)}. Per-account rates
              and their inferred limits are on the Accounts tab, where the limit applies.`
            : "Requests per minute across the whole pool."}
        </CardDescription>
      </CardHeader>
      <CardContent className="flex-1">
        {isLoading || !rate ? (
          <ChartSkeleton rows={1} />
        ) : !hasTraffic ? (
          <EmptyState
            icon={Activity}
            title="No traffic in this window"
            description="The chart fills in as requests reach /v1."
          />
        ) : (
          <div className="space-y-4">
            <div className="relative">
              <svg
                viewBox={`0 0 ${lastIndex} ${CHART_HEIGHT}`}
                preserveAspectRatio="none"
                className="h-36 w-full"
                role="img"
                aria-label={`Total requests per minute across all accounts. Peak ${round(
                  totals.peakPerMinute,
                )} per minute, average ${round(totals.meanPerMinute)} per minute, ${totals.requests} requests in the window.`}
              >
                <path d={area} fill="var(--chart-1)" opacity={0.26} />
                <path
                  d={line}
                  fill="none"
                  stroke="var(--chart-1)"
                  strokeWidth={1}
                  vectorEffect="non-scaling-stroke"
                />
                {/* The mean makes a spike legible as a spike rather than as the
                    normal level, which a bare area chart cannot convey. */}
                <line
                  x1={0}
                  x2={lastIndex}
                  y1={meanY}
                  y2={meanY}
                  stroke="var(--muted-foreground)"
                  strokeWidth={1}
                  strokeDasharray="4 3"
                  opacity={0.7}
                  vectorEffect="non-scaling-stroke"
                />
                {/* Rejections are drawn on top: they are rare and must not be
                    lost inside the total they are part of. */}
                {totals.rateLimitedPerMinute.some(Boolean) && (
                  <path
                    d={totals.rateLimitedPerMinute
                      .map((value, index) => `${index === 0 ? "M" : "L"} ${index} ${y(value)}`)
                      .join(" ")}
                    fill="none"
                    stroke="var(--destructive)"
                    strokeWidth={1}
                    vectorEffect="non-scaling-stroke"
                  />
                )}
              </svg>
              <span
                className="pointer-events-none absolute left-0 -translate-y-1/2 rounded bg-background/80 px-1 text-[10px] tabular-nums text-muted-foreground"
                style={{ top: `${(meanY / CHART_HEIGHT) * 100}%` }}
              >
                avg {round(totals.meanPerMinute)}/min
              </span>
            </div>

            {/* Container queries, not viewport ones: this panel sits full-width on
                its own and half-width beside the token chart, so the column count
                has to follow the card rather than the screen. */}
            <dl className="grid grid-cols-2 gap-3 border-t pt-4 @md/panel:grid-cols-3 @2xl/panel:grid-cols-5">
              <Figure
                label="Peak"
                value={`${round(totals.peakPerMinute)}/min`}
                hint={`burst ${totals.peakConcurrentRpm}`}
              />
              <Figure label="Average" value={`${round(totals.meanPerMinute)}/min`} />
              <Figure label="Requests" value={totals.requests.toLocaleString()} hint="in window" />
              <Figure
                label="Rejected"
                value={totals.rateLimited.toLocaleString()}
                tone={totals.rateLimited > 0 ? "warning" : undefined}
                hint={throttled.length > 0 ? `${throttled.length} account${throttled.length === 1 ? "" : "s"}` : undefined}
              />
              <Figure
                label="Failed"
                value={totals.failures.toLocaleString()}
                tone={totals.failures > 0 ? "warning" : undefined}
              />
            </dl>

            {totals.rateLimited > 0 && (
              <p className="text-xs text-destructive">
                {totals.rateLimited} upstream rejection{totals.rateLimited === 1 ? "" : "s"} on{" "}
                {throttled.join(", ")}. A rejection failover recovered from is still a success to the client, so it does
                not appear in the request log.
              </p>
            )}

            <p className="text-xs text-muted-foreground">
              Peak is the busiest bucket scaled to a minute; burst sums each account&apos;s own sliding-window rate, which
              is what an upstream rate limit actually measures.
            </p>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
