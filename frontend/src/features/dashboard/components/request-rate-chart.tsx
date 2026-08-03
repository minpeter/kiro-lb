import { useMemo } from "react";
import { Activity } from "lucide-react";
import { Area } from "@/components/dither-kit/area";
import { AreaChart } from "@/components/dither-kit/area-chart";
import { ReferenceLine } from "@/components/dither-kit/reference-line";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/empty-state";
import { accountRateSeries } from "../dither-series";
import type { AccountRateSeries } from "../types";
import type { RequestRate } from "../types";
import { ChartSkeleton } from "./skeletons";

function formatClock(unixSeconds: number): string {
  return new Date(unixSeconds * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function AccountRatePanel({ series }: { series: AccountRateSeries }) {
  const view = useMemo(() => accountRateSeries(series), [series]);

  return (
    <div className="space-y-2 rounded-lg border p-3">
      <div className="flex items-baseline justify-between gap-2">
        <span className="font-mono text-xs">{series.account}</span>
        <span className="text-xs tabular-nums text-muted-foreground">
          {view.peak}/min peak
          {view.load !== null && (
            <span className={view.nearLimit ? "text-destructive" : undefined}>
              {" "}
              · {Math.round(view.load * 100)}% of limit
            </span>
          )}
          {view.rejected > 0 && <span className="text-destructive"> · {view.rejected} rejected</span>}
        </span>
      </div>

      {/* An idle account keeps its panel: the grid must not change shape with
          traffic, or an account that served nothing looks like one that is not
          in the pool at all. */}
      {!view.hasTraffic ? (
        <div className="flex h-28 items-center justify-center rounded-md border border-dashed border-border/60">
          <p className="text-xs text-muted-foreground">No traffic in this window</p>
        </div>
      ) : (
        <div className="relative">
          <div
            className="h-28 w-full"
            role="img"
            aria-label={`Peak requests per minute for account ${series.account}: ${view.peak} per minute${
              series.limitRpm === null ? ", no observed limit" : `, observed limit ${series.limitRpm} per minute`
            }`}
          >
            <AreaChart
              data={view.rows}
              config={view.config}
              interactive={false}
              animate={false}
              yMax={view.yMax}
              margins={{ top: 6, right: 2, bottom: 2, left: 2 }}
            >
              <Area dataKey="rpm" variant="gradient" />
              {series.limitRpm !== null && (
                <ReferenceLine y={series.limitRpm} className="stroke-destructive/70" />
              )}
            </AreaChart>
          </div>
          {series.limitRpm !== null && (
            <Badge variant="destructive" className="absolute right-1 top-1 text-[10px]">
              ~{series.limitRpm}/min
            </Badge>
          )}
        </div>
      )}

      <p className="text-xs text-muted-foreground">
        {series.limitRpm === null ? (
          <>
            No guide yet: {series.limitUnknownReason}.
            {series.safeRpm > 0 && ` Served ${series.safeRpm}/min without rejection.`}
          </>
        ) : view.nearLimit ? (
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
  // Emptiness is a per-account fact, so it is reported inside each panel. The
  // card only collapses when there is no account to chart at all.
  const hasAccounts = (rate?.accounts.length ?? 0) > 0;

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
        ) : !hasAccounts ? (
          <EmptyState
            icon={Activity}
            title="No accounts to chart"
            description="Rate history appears once an account joins the pool."
          />
        ) : (
          <div className="grid gap-3 lg:grid-cols-2">
            {rate.accounts.map((series) => (
              <AccountRatePanel key={series.account} series={series} />
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
