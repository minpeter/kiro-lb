import { type ReactNode } from "react";
import { Radio, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

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

type AppHeaderProps = {
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
    </div>
  );
}

export function AppHeader({
  isMutating,
  isLive,
  lastUpdatedAt,
  onToggleLive,
  onRefresh,
  onSignOut,
}: AppHeaderProps) {
  return (
    <header className="sticky top-0 z-10 border-b bg-background/90 backdrop-blur">
      <div className="mx-auto flex max-w-7xl 2xl:max-w-[100rem] flex-col gap-3 px-4 py-3 sm:flex-row sm:items-center sm:justify-between sm:gap-4 sm:px-6 sm:py-4">
        <div className="min-w-0 space-y-2 sm:flex sm:items-center sm:gap-3 sm:space-y-0">
          <div className="flex shrink-0 items-center gap-3 whitespace-nowrap">
            <KiroLogo />
            <p className="font-semibold leading-none">Kiro-LB</p>
          </div>
        </div>
        <div className="flex w-full items-center gap-2 sm:w-auto sm:shrink-0">
          <LiveToggle isLive={isLive} lastUpdatedAt={lastUpdatedAt} onToggle={onToggleLive} />
          <Button variant="outline" size="sm" disabled={isMutating} onClick={onRefresh}>
            <RefreshCw className={isMutating ? "animate-spin" : ""} />
            <span className="sm:hidden">Refresh</span>
            <span className="hidden sm:inline">Refresh usage</span>
          </Button>
          <Button variant="outline" size="sm" className="ml-auto sm:ml-0" onClick={onSignOut}>
            Sign out
          </Button>
        </div>
      </div>
    </header>
  );
}
