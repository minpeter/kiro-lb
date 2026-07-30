import type { ReactNode } from "react";
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

function HeaderFacts({ overview, isLoading }: { overview?: Overview; isLoading: boolean }) {
  if (isLoading) return <Skeleton className="h-3.5 w-64" />;
  if (!overview) return null;
  const healthy = overview.proxy.status === "healthy";
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
      <span className="flex items-center gap-1.5">
        <span aria-hidden className={`size-1.5 rounded-full ${healthy ? "bg-emerald-500" : "bg-destructive"}`} />
        {overview.proxy.status}
      </span>
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
  onToggleLive: () => void;
  onRefresh: () => void;
  onSignOut: () => void;
};

function LiveToggle({ isLive, lastUpdatedAt, onToggle }: { isLive: boolean; lastUpdatedAt?: number; onToggle: () => void }) {
  const stamp = lastUpdatedAt ? new Date(lastUpdatedAt).toLocaleTimeString() : "never";
  return (
    <Button
      variant={isLive ? "secondary" : "outline"}
      size="sm"
      onClick={onToggle}
      aria-pressed={isLive}
      title={`Last updated ${stamp}`}
    >
      <Radio className={isLive ? "animate-pulse text-emerald-500" : ""} />
      {isLive ? "Live" : "Paused"}
    </Button>
  );
}

export function AppHeader({
  overview,
  isLoading,
  isMutating,
  isLive,
  lastUpdatedAt,
  onToggleLive,
  onRefresh,
  onSignOut,
}: AppHeaderProps) {
  return (
    <header className="sticky top-0 z-10 border-b bg-background/90 backdrop-blur">
      <div className="mx-auto flex max-w-7xl items-center justify-between gap-4 px-6 py-4">
        <div className="flex items-center gap-3">
          <KiroLogo />
          <div className="space-y-1">
            <p className="font-semibold leading-none">Kiro-LB</p>
            <HeaderFacts overview={overview} isLoading={isLoading} />
          </div>
        </div>
        <div className="flex items-center gap-2">
          <LiveToggle isLive={isLive} lastUpdatedAt={lastUpdatedAt} onToggle={onToggleLive} />
          <Button variant="outline" size="sm" disabled={isMutating} onClick={onRefresh}>
            <RefreshCw className={isMutating ? "animate-spin" : ""} />
            Refresh usage
          </Button>
          <Button variant="ghost" size="sm" onClick={onSignOut}>
            Sign out
          </Button>
        </div>
      </div>
    </header>
  );
}
