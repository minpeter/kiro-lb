import { Activity, Coins, Gauge, ServerCog, ShieldCheck, TriangleAlert, X } from "lucide-react";
import { useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { dashboardApi } from "@/features/dashboard/api";
import { exactTokens, formatLatency, formatTokens, summarizeUsage } from "@/features/dashboard/format";
import { deriveOverviewKpis } from "@/features/dashboard/overview-kpis";
import { useDashboard } from "@/features/dashboard/use-dashboard";
import { useTabHash } from "@/features/dashboard/use-tab-hash";
import { AccountsPanel } from "@/features/dashboard/components/accounts-panel";
import { ApiKeysPanel } from "@/features/dashboard/components/api-keys-panel";
import { CreateKeyDialog } from "@/features/dashboard/components/create-key-dialog";
import { DeviceLoginCard } from "@/features/dashboard/components/device-login-card";
import { LoginCard } from "@/features/dashboard/components/login-card";
import { RequestLogTable } from "@/features/dashboard/components/request-log-table";
import { RequestRateChart } from "@/features/dashboard/components/request-rate-chart";
import { TokenUsagePanel } from "@/features/dashboard/components/token-usage-panel";
import { AccountTokenPanel } from "@/features/dashboard/components/account-token-panel";
import { TotalRateChart } from "@/features/dashboard/components/total-rate-chart";
import { AppHeader, KiroLogo, StatCard } from "@/features/dashboard/components/shell";
import { StatCardSkeleton } from "@/features/dashboard/components/skeletons";

export default function App() {
  const dashboard = useDashboard();
  const [tab, selectTab] = useTabHash();
  const [isCreateKeyOpen, setIsCreateKeyOpen] = useState(false);
  const { overview, isLoading, isMutating, runAction } = dashboard;
  // Totals are derived from the same per-key usage the API keys tab shows, so
  // the two views can never disagree.
  const totals = useMemo(() => summarizeUsage(dashboard.keyUsage), [dashboard.keyUsage]);
  const kpis = useMemo(
    () => (overview ? deriveOverviewKpis(dashboard.accounts, overview) : undefined),
    [dashboard.accounts, overview],
  );

  if (!dashboard.isAuthenticated && isLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background text-muted-foreground">
        <div role="status" className="flex items-center gap-3 text-sm">
          <KiroLogo />
          <span>Loading dashboard…</span>
        </div>
      </div>
    );
  }

  if (!dashboard.isAuthenticated) {
    // A cold-start outage should not present as a silent login screen: surface
    // the non-auth failure the hook kept out of the auth error slot.
    return <LoginCard error={dashboard.error || dashboard.connectionError || ""} onSignIn={dashboard.signIn} />;
  }

  const createKey = async (name: string) => {
    const created = await dashboardApi.createApiKey(name);
    await dashboard.reload();
    return created.apiKey;
  };

  return (
    <div className="min-h-screen bg-background">
      <AppHeader
        overview={overview}
        isLoading={isLoading}
        isMutating={isMutating}
        isLive={dashboard.isLive}
        lastUpdatedAt={dashboard.lastUpdatedAt}
        routableAccounts={kpis?.routableAccounts}
        onToggleLive={() => dashboard.setIsLive(!dashboard.isLive)}
        onRefresh={() => void runAction(dashboardApi.refreshUsage)}
        onSignOut={() => void dashboard.signOut()}
      />

      {dashboard.connectionError && (
        <div role="status" aria-live="polite" className="border-b border-warning/30 bg-warning/10 text-warning">
          <div className="mx-auto flex max-w-7xl items-center justify-between gap-3 px-4 py-2 text-sm sm:px-6">
            <span className="flex items-center gap-2 font-medium">
              <TriangleAlert size={15} aria-hidden />
              Connection lost - retrying
            </span>
            <Button variant="outline" size="sm" onClick={() => void dashboard.reload()}>
              Retry
            </Button>
          </div>
        </div>
      )}

      {dashboard.actionError && (
        <div role="alert" className="border-b border-destructive/30 bg-destructive/10 text-destructive">
          <div className="mx-auto flex max-w-7xl items-center justify-between gap-3 px-4 py-2 text-sm sm:px-6">
            <span className="flex items-center gap-2">
              <TriangleAlert size={15} aria-hidden />
              {dashboard.actionError}
            </span>
            <Button
              variant="ghost"
              size="icon"
              className="size-8 shrink-0"
              aria-label="Dismiss action error"
              onClick={dashboard.clearActionError}
            >
              <X aria-hidden />
            </Button>
          </div>
        </div>
      )}

      <main className="mx-auto max-w-7xl p-4 sm:p-6">
        <Tabs value={tab} onValueChange={selectTab} className="space-y-6">
          <TabsList>
            <TabsTrigger value="overview">Overview</TabsTrigger>
            <TabsTrigger value="accounts">Accounts</TabsTrigger>
            <TabsTrigger value="keys">API keys</TabsTrigger>
          </TabsList>

          <TabsContent value="overview" className="space-y-6">
            <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-5">
              {isLoading || !overview ? (
                Array.from({ length: 5 }).map((_, index) => <StatCardSkeleton key={index} />)
              ) : (
                <>
                  <StatCard
                    label="Total tokens"
                    value={<span title={exactTokens(totals.totalTokens)}>{formatTokens(totals.totalTokens)}</span>}
                    icon={<Coins size={15} />}
                  />
                  <StatCard label="24h requests" value={overview.requests24h.toLocaleString()} icon={<Activity size={15} />} />
                  <StatCard
                    label="24h success"
                    value={
                      <span
                        className={kpis?.success.isCritical ? "text-destructive" : undefined}
                        title={`${overview.successes24h.toLocaleString()} successful of ${overview.requests24h.toLocaleString()} requests`}
                      >
                        {kpis?.success.label}
                      </span>
                    }
                    icon={<ShieldCheck size={15} className={kpis?.success.isCritical ? "text-destructive" : undefined} />}
                  />
                  <StatCard
                    label="Average latency"
                    value={
                      kpis?.maskAverageLatency ? (
                        <span title="all recent requests failed">—</span>
                      ) : (
                        formatLatency(overview.averageLatencyMs)
                      )
                    }
                    icon={<Gauge size={15} />}
                  />
                  <StatCard
                    label="Routable accounts"
                    value={
                      <span
                        className={kpis?.routableAccounts.isCritical ? "text-destructive" : undefined}
                        title={`${kpis?.routableAccounts.count ?? 0} routable of ${kpis?.routableAccounts.total ?? 0} total accounts`}
                      >
                        {kpis?.routableAccounts.count}/{kpis?.routableAccounts.total}
                      </span>
                    }
                    icon={
                      <ServerCog
                        size={15}
                        className={kpis?.routableAccounts.isCritical ? "text-destructive" : undefined}
                      />
                    }
                  />
                </>
              )}
            </section>

            {/* Side by side once there is room for both: the rate chart answers
                what the pool is doing now, the donut what it has spent, and
                reading them together is the point of this tab. They stack below
                xl, where half a screen is too narrow for the donut and legend.
                Overview stays pool-wide; the per-account breakdown and its
                inferred limits live on the Accounts tab, where a limit applies. */}
            <section className="grid items-stretch gap-6 xl:grid-cols-2">
              <TotalRateChart rate={dashboard.rate} isLoading={isLoading} />
              <TokenUsagePanel keyUsage={dashboard.keyUsage} isLoading={isLoading} />
            </section>

            <RequestLogTable
              page={dashboard.logs}
              isLoading={isLoading || dashboard.isLogsLoading}
              onLimitChange={dashboard.setLogLimit}
              onOffsetChange={dashboard.setLogOffset}
            />
          </TabsContent>

          <TabsContent value="accounts" className="space-y-6">
            <AccountsPanel
              accounts={dashboard.accounts}
              isLoading={isLoading}
              isMutating={isMutating}
              onDeleteAccount={(id) => void runAction(() => dashboardApi.deleteAccount(id))}
            />
            {/* Placed here rather than on Overview for the reason stated above:
                Overview stays pool-wide, and this is a per-account breakdown. It
                pairs with the quota column in the panel above - that one is what
                Kiro counts, this one is what the gateway measured. */}
            <AccountTokenPanel accountTokenUsage={dashboard.accountTokenUsage} isLoading={isLoading} />
            <RequestRateChart rate={dashboard.rate} isLoading={isLoading} />
            <DeviceLoginCard onRegistered={dashboard.reload} />
          </TabsContent>

          <TabsContent value="keys">
            <ApiKeysPanel
              apiKeys={dashboard.apiKeys}
              keyUsage={dashboard.keyUsage}
              isLoading={isLoading}
              isMutating={isMutating}
              onCreate={() => setIsCreateKeyOpen(true)}
              onRevoke={(id) => void runAction(() => dashboardApi.revokeApiKey(id))}
            />
          </TabsContent>
        </Tabs>
      </main>

      <CreateKeyDialog open={isCreateKeyOpen} onOpenChange={setIsCreateKeyOpen} onCreate={createKey} />
    </div>
  );
}
