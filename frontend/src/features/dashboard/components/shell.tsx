import { useEffect, useState, type ReactNode } from "react";
import { Radio, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDuration } from "../format";
import type { Overview } from "../types";

export function KiroLogo({ size = 36 }: { size?: number }) {
  return <img src="/kiro-icon.svg" width={size} height={size} alt="" className="rounded-lg" />;
}

export function StatCard({ label, value, icon }: { label: string; value: ReactNode; icon: ReactNode }) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardDescription className="flex items-center gap-2">
          {icon}
          {label}
        </CardDescription>
        <CardTitle className="text-3xl tabular-nums">{value}</CardTitle>
      </CardHeader>
    </Card>
  );
}

function HeaderFacts({
  overview,
  isLoading,
  routableAccounts,
}: {
  overview?: Overview;
  isLoading: boolean;
  routableAccounts?: { count: number; total: number };
}) {
  if (isLoading) return <Skeleton className="h-3.5 w-64 max-w-full" />;
  if (!overview) return null;
  const healthy = overview.proxy.status === "healthy";
  const isRoutabilityDegraded = healthy && routableAccounts?.count === 0;
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
      <span className="flex items-center gap-1.5">
        <span aria-hidden className={`size-1.5 rounded-full ${healthy ? "bg-success" : "bg-destructive"}`} />
        {overview.proxy.status}
      </span>
      {isRoutabilityDegraded && (
        <span className="rounded-full border border-warning/40 bg-warning/10 px-2 py-0.5 font-medium text-warning">
          0 routable
        </span>
      )}
      <span>up {formatDuration(overview.proxy.uptimeSeconds)}</span>
      <span>
        {overview.accounts.initialized}/{overview.accounts.total} accounts
      </span>
      <span>{overview.models} models</span>
    </div>
  );
}

export type AppHeaderProps = {
  overview?: Overview;
  isLoading: boolean;
  isMutating: boolean;
  isLive: boolean;
  lastUpdatedAt?: number;
  routableAccounts?: { count: number; total: number };
  onToggleLive: () => void;
  onRefresh: () => void;
  onSignOut: () => void;
};

function LiveToggle({ isLive, lastUpdatedAt, onToggle }: { isLive: boolean; lastUpdatedAt?: number; onToggle: () => void }) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 10_000);
    return () => window.clearInterval(timer);
  }, []);

  const stamp = lastUpdatedAt ? new Date(lastUpdatedAt).toLocaleTimeString() : "never";
  const ageSeconds = lastUpdatedAt ? Math.max(0, Math.floor((now - lastUpdatedAt) / 1000)) : null;
  return (
    <div className="flex min-w-0 items-center gap-2">
      <Button
        variant={isLive ? "secondary" : "outline"}
        size="sm"
        onClick={onToggle}
        aria-pressed={isLive}
        title={`Last updated ${stamp}`}
      >
        <Radio className={isLive ? "animate-pulse text-success" : ""} />
        {isLive ? "Live" : "Paused"}
      </Button>
      {/* No aria-live: announcing every refresh would chatter at screen readers; the Live button's title carries the stamp. */}
      <span className="whitespace-nowrap text-xs text-muted-foreground">
        {ageSeconds === null ? "not updated" : `updated ${ageSeconds}s ago`}
      </span>
    </div>
  );
}

export function AppHeader({
  overview,
  isLoading,
  isMutating,
  isLive,
  lastUpdatedAt,
  routableAccounts,
  onToggleLive,
  onRefresh,
  onSignOut,
}: AppHeaderProps) {
  return (
    <header className="sticky top-0 z-10 border-b bg-background/90 backdrop-blur">
      <div className="mx-auto flex max-w-7xl flex-col gap-3 px-4 py-3 sm:flex-row sm:items-center sm:justify-between sm:gap-4 sm:px-6 sm:py-4">
        <div className="min-w-0 space-y-2 sm:flex sm:items-center sm:gap-3 sm:space-y-0">
          <div className="flex shrink-0 items-center gap-3 whitespace-nowrap">
            <KiroLogo />
            <p className="font-semibold leading-none">Kiro-LB</p>
          </div>
          <HeaderFacts overview={overview} isLoading={isLoading} routableAccounts={routableAccounts} />
        </div>
        <div className="flex w-full items-center gap-2 sm:w-auto sm:shrink-0">
          <LiveToggle isLive={isLive} lastUpdatedAt={lastUpdatedAt} onToggle={onToggleLive} />
          <Button variant="outline" size="sm" disabled={isMutating} onClick={onRefresh}>
            <RefreshCw className={isMutating ? "animate-spin" : ""} />
            <span className="sm:hidden">Refresh</span>
            <span className="hidden sm:inline">Refresh usage</span>
          </Button>
          <Button variant="ghost" size="sm" className="ml-auto sm:ml-0" onClick={onSignOut}>
            Sign out
          </Button>
        </div>
      </div>
    </header>
  );
}
