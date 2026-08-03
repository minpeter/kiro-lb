import { Activity, Coins, Gauge, ServerCog, ShieldCheck } from "lucide-react";
import { useMemo } from "react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { dashboardApi } from "@/features/dashboard/api";
import { exactTokens, formatLatency, formatTokens, summarizeUsage } from "@/features/dashboard/format";
import { useDashboard } from "@/features/dashboard/use-dashboard";
import { useTabHash } from "@/features/dashboard/use-tab-hash";
import { AccountsPanel } from "@/features/dashboard/components/accounts-panel";
import { ApiKeysPanel } from "@/features/dashboard/components/api-keys-panel";
import { DeviceLoginCard } from "@/features/dashboard/components/device-login-card";
import { LoginCard } from "@/features/dashboard/components/login-card";
import { RequestLogTable } from "@/features/dashboard/components/request-log-table";
import { RequestRateChart } from "@/features/dashboard/components/request-rate-chart";
import { TokenUsagePanel } from "@/features/dashboard/components/token-usage-panel";
import { TotalRateChart } from "@/features/dashboard/components/total-rate-chart";
import { AppHeader, StatCard } from "@/features/dashboard/components/shell";
import { StatCardSkeleton } from "@/features/dashboard/components/skeletons";

export default function App() {
  const dashboard = useDashboard();
  const [tab, selectTab] = useTabHash();
  const { overview, isLoading, isMutating, runAction } = dashboard;
  // Totals are derived from the same per-key usage the API keys tab shows, so
  // the two views can never disagree.
  const totals = useMemo(() => summarizeUsage(dashboard.keyUsage), [dashboard.keyUsage]);

  if (!dashboard.isAuthenticated && !isLoading) {
    return <LoginCard error={dashboard.error} onSignIn={dashboard.signIn} />;
  }

  const createKey = () => {
    const name = window.prompt("Key name");
    if (name === null) return;
    void runAction(async () => {
      const created = await dashboardApi.createApiKey(name);
      window.prompt("Copy this API key now. It cannot be displayed again.", created.apiKey);
    });
  };

  return (
    <div className="min-h-screen bg-background">
      <AppHeader
        overview={overview}
        isLoading={isLoading}
        isMutating={isMutating}
        isLive={dashboard.isLive}
        lastUpdatedAt={dashboard.lastUpdatedAt}
        onToggleLive={() => dashboard.setIsLive(!dashboard.isLive)}
        onRefresh={() => void runAction(dashboardApi.refreshUsage)}
        onSignOut={() => void dashboard.signOut()}
      />

      <main className="mx-auto max-w-7xl p-6">
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
                  <StatCard label="24h success" value={overview.successes24h.toLocaleString()} icon={<ShieldCheck size={15} />} />
                  <StatCard label="Average latency" value={formatLatency(overview.averageLatencyMs)} icon={<Gauge size={15} />} />
                  <StatCard
                    label="Ready accounts"
                    value={`${overview.accounts.initialized}/${overview.accounts.total}`}
                    icon={<ServerCog size={15} />}
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
            <AccountsPanel accounts={dashboard.accounts} isLoading={isLoading} />
            <RequestRateChart rate={dashboard.rate} isLoading={isLoading} />
            <DeviceLoginCard onRegistered={dashboard.reload} />
          </TabsContent>

          <TabsContent value="keys">
            <ApiKeysPanel
              apiKeys={dashboard.apiKeys}
              keyUsage={dashboard.keyUsage}
              isLoading={isLoading}
              isMutating={isMutating}
              onCreate={createKey}
              onRevoke={(id) => void runAction(() => dashboardApi.revokeApiKey(id))}
            />
          </TabsContent>
        </Tabs>
      </main>
    </div>
  );
}
