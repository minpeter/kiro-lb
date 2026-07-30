import { Activity, Gauge, ServerCog, ShieldCheck } from "lucide-react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { dashboardApi } from "@/features/dashboard/api";
import { formatLatency } from "@/features/dashboard/format";
import { useDashboard } from "@/features/dashboard/use-dashboard";
import { useTabHash } from "@/features/dashboard/use-tab-hash";
import { AccountsPanel } from "@/features/dashboard/components/accounts-panel";
import { ApiKeysPanel } from "@/features/dashboard/components/api-keys-panel";
import { LoginCard } from "@/features/dashboard/components/login-card";
import { RegisterAccountCard } from "@/features/dashboard/components/register-account-card";
import { RequestLogTable } from "@/features/dashboard/components/request-log-table";
import { RequestRateChart } from "@/features/dashboard/components/request-rate-chart";
import { AppHeader, StatCard } from "@/features/dashboard/components/shell";
import { StatCardSkeleton } from "@/features/dashboard/components/skeletons";

export default function App() {
  const dashboard = useDashboard();
  const [tab, selectTab] = useTabHash();
  const { overview, isLoading, isMutating, runAction } = dashboard;

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
            <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
              {isLoading || !overview ? (
                Array.from({ length: 4 }).map((_, index) => <StatCardSkeleton key={index} />)
              ) : (
                <>
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

            <RequestRateChart rate={dashboard.rate} isLoading={isLoading} />

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
            <RegisterAccountCard onRegistered={dashboard.reload} />
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
