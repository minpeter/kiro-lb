import { useMemo } from "react";
import { Activity } from "lucide-react";
import { Area } from "@/components/dither-kit/area";
import { AreaChart } from "@/components/dither-kit/area-chart";
import { Grid } from "@/components/dither-kit/grid";
import { ReferenceLine } from "@/components/dither-kit/reference-line";
import { Tooltip } from "@/components/dither-kit/tooltip";
import { XAxis } from "@/components/dither-kit/x-axis";
import { YAxis } from "@/components/dither-kit/y-axis";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/empty-state";
import { ChartSkeleton } from "./skeletons";
import { rateChartConfig, rateChartRows } from "../dither-series";
import { summarizeRate, throttledAccounts } from "../request-rate-totals";
import { PANEL_UPPER_MIN_HEIGHT } from "../panel-metrics";
import type { RequestRate } from "../types";

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
  const rows = useMemo(() => rateChartRows(totals), [totals]);
  const config = useMemo(() => rateChartConfig(totals), [totals]);

  const hasTraffic = totals.requests > 0;

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
            {/* Floor the plot area so this card's divider lines up with the token
                panel's when the two share a row. */}
            <div className={`flex flex-col justify-center ${PANEL_UPPER_MIN_HEIGHT}`}>
              <div
                className="h-44 w-full"
                role="img"
                aria-label={`Total requests per minute across all accounts. Peak ${round(
                  totals.peakPerMinute,
                )} per minute, average ${round(totals.meanPerMinute)} per minute, ${totals.requests} requests in the window.`}
              >
                {/* No entrance sweep: live polling bumps the data revision every
                    second, so the reveal would replay forever instead of playing once. */}
                <AreaChart data={rows} config={config} bloom="low" bloomOnHover animate={false}>
                  <Grid />
                  <XAxis dataKey="at" maxTicks={6} />
                  <YAxis tickCount={3} tickFormatter={round} />
                  {/* The mean makes a spike legible as a spike rather than as the
                      normal level, which a bare area chart cannot convey. */}
                  <ReferenceLine y={totals.meanPerMinute} label={`avg ${round(totals.meanPerMinute)}/min`} />
                  <Area dataKey="served" variant="gradient" />
                  {/* Rejections are drawn on top: they are rare and must not be
                      lost inside the total they are part of. */}
                  {config.rejected && <Area dataKey="rejected" variant="hatched" />}
                  <Tooltip labelKey="at" valueFormatter={(value) => `${round(value)}/min`} />
                </AreaChart>
              </div>
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
