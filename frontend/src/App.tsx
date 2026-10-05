import {
  Activity,
  Coins,
  CreditCard,
  ServerCog,
  ShieldCheck,
  Wallet,
  Info,
  KeyRound,
  LayoutDashboard,
  Settings,
  TriangleAlert,
  Users,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { dashboardApi } from "@/features/dashboard/api";
import { exactTokens, formatTokens, summarizeUsage } from "@/features/dashboard/format";
import { creditTotals } from "@/features/dashboard/credit-totals";
import { deriveOverviewKpis } from "@/features/dashboard/overview-kpis";
import { useDashboard } from "@/features/dashboard/use-dashboard";
import { useTabHash } from "@/features/dashboard/use-tab-hash";
import { AccountsPanel } from "@/features/dashboard/components/accounts-panel";
import { ApiKeysPanel } from "@/features/dashboard/components/api-keys-panel";
import { InfoPanel } from "@/features/dashboard/components/info-panel";
import { SettingsPanel } from "@/features/dashboard/components/settings-panel";
import { CreateKeyDialog } from "@/features/dashboard/components/create-key-dialog";
import { DeviceLoginCard } from "@/features/dashboard/components/device-login-card";
import { LoginCard } from "@/features/dashboard/components/login-card";
import { RequestLogTable } from "@/features/dashboard/components/request-log-table";
import { TokenUsagePanel } from "@/features/dashboard/components/token-usage-panel";
import { AccountTokenPanel } from "@/features/dashboard/components/account-token-panel";
import { TotalRateChart } from "@/features/dashboard/components/total-rate-chart";
import { AppearancePanel } from "@/features/dashboard/components/appearance-panel";
import { usePreferences } from "@/features/dashboard/preferences";
import { KiroLbWordmark, SignOutButton, KiroLogo, StatCard } from "@/features/dashboard/components/shell";
import { StatCardSkeleton } from "@/features/dashboard/components/skeletons";
import { AlertStack } from "@/features/dashboard/components/alert-stack";
import { dismissAlert, pushAlert } from "@/features/dashboard/alerts";
import { useMediaQuery } from "@/features/dashboard/use-media-query";

// Quota moves slowly, so this is deliberately far apart: each tick is a real
// call to Kiro for every account.
const USAGE_REFRESH_MS = 5 * 60 * 1000;

const formatCreditTotal = (n: number) => n.toLocaleString(undefined, { maximumFractionDigits: 1 });

export default function App() {
  const dashboard = useDashboard();
  const { t } = usePreferences();
  const credits = useMemo(() => creditTotals(dashboard.accounts), [dashboard.accounts]);
  const [tab, selectTab] = useTabHash();
  const [isCreateKeyOpen, setIsCreateKeyOpen] = useState(false);
  const { overview, isLoading, isMutating, runAction, isAuthenticated, isLive, refreshUsageQuietly } =
    dashboard;
  // Totals are derived from the same per-key usage the API keys tab shows, so
  // the two views can never disagree.
  const totals = useMemo(() => summarizeUsage(dashboard.keyUsage), [dashboard.keyUsage]);
  const kpis = useMemo(
    () => (overview ? deriveOverviewKpis(dashboard.accounts, overview) : undefined),
    [dashboard.accounts, overview],
  );

  // Refresh quota on a timer, quietly: no spinner and no full reload, so panels
  // do not repaint. The button animation stays reserved for a manual refresh.
  const mutatingRef = useRef(isMutating);
  useEffect(() => {
    mutatingRef.current = isMutating;
  }, [isMutating]);
  useEffect(() => {
    if (!isAuthenticated || !isLive) return;
    const timer = window.setInterval(() => {
      // Read through a ref so a mutation does not re-arm the timer, which is
      // what turned one scheduled refresh into a burst of them.
      if (!mutatingRef.current) void refreshUsageQuietly();
    }, USAGE_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [isAuthenticated, isLive, refreshUsageQuietly]);

  const { actionError, actionNotice, clearActionError, clearActionNotice } = dashboard;
  useEffect(() => {
    if (!actionError) return;
    pushAlert({ tone: "error", error: actionError });
    clearActionError();
  }, [actionError, clearActionError]);
  useEffect(() => {
    if (!actionNotice) return;
    pushAlert({ tone: "success", text: actionNotice });
    clearActionNotice();
  }, [actionNotice, clearActionNotice]);

  const signInError = isAuthenticated ? "" : dashboard.error || dashboard.connectionError || "";
  const signInAlert = useRef<number | undefined>(undefined);
  useEffect(() => {
    if (signInError) signInAlert.current = pushAlert({ tone: "error", error: signInError });
  }, [signInError]);
  useEffect(() => {
    if (isAuthenticated && signInAlert.current !== undefined) {
      dismissAlert(signInAlert.current);
      signInAlert.current = undefined;
    }
  }, [isAuthenticated]);
  const wide = useMediaQuery("(min-width: 768px)");

  if (!dashboard.isAuthenticated && isLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background text-muted-foreground">
        <div role="status" className="flex items-center gap-3 text-sm">
          <KiroLogo />
          <span>{t("loadingDashboard")}</span>
        </div>
      </div>
    );
  }

  if (!dashboard.isAuthenticated) {
    // A cold-start outage should not present as a silent login screen: surface
    // the non-auth failure the hook kept out of the auth error slot.
    return (
      <>
        <AlertStack />
        <LoginCard error="" onSignIn={dashboard.signIn} />
      </>
    );
  }

  const createKey = async (name: string) => {
    const created = await dashboardApi.createApiKey(name);
    await dashboard.reload();
    return created.apiKey;
  };

  const navGroups = [
    { label: t("navMonitoring"), items: [{ value: "overview", label: t("overview"), icon: LayoutDashboard }] },
    {
      label: t("navManagement"),
      items: [
        { value: "accounts", label: t("accounts"), icon: Users },
        { value: "keys", label: t("apiKeys"), icon: KeyRound },
      ],
    },
    {
      label: t("navSystem"),
      items: [
        { value: "settings", label: t("settings"), icon: Settings },
        { value: "info", label: t("info"), icon: Info },
      ],
    },
  ];
  const navItems = navGroups.flatMap((group) => group.items);
  const signOut = () => void dashboard.signOut();

  return (
    <Tabs value={tab} onValueChange={selectTab} orientation={wide ? "vertical" : "horizontal"} className="min-h-screen flex-row! gap-0 bg-background">
      <aside className="sticky top-0 hidden h-screen w-56 shrink-0 flex-col gap-6 border-r bg-muted/30 px-3 py-5 md:flex">
        <div className="px-2">
          <KiroLbWordmark />
        </div>
        <TabsList aria-label={t("navigation")} className="h-auto w-full flex-col items-stretch gap-5 bg-transparent p-0">
          {navGroups.map((group) => (
            <div key={group.label} className="flex flex-col gap-1">
              <span className="px-3 pb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                {group.label}
              </span>
              {group.items.map(({ value, label, icon: Icon }) => (
                <TabsTrigger
                  key={value}
                  value={value}
                  className="h-9 flex-none justify-start gap-3 px-3 data-[state=active]:bg-background data-[state=active]:shadow-sm"
                >
                  <Icon aria-hidden />
                  {label}
                </TabsTrigger>
              ))}
            </div>
          ))}
        </TabsList>
        <div className="mt-auto border-t pt-4">
          <SignOutButton vertical onSignOut={signOut} />
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">

      {dashboard.connectionError && (
        <div role="status" aria-live="polite" className="border-b border-warning/30 bg-warning/10 text-warning">
          <div className="flex items-center justify-between gap-3 px-4 py-2 text-sm sm:px-6">
            <span className="flex items-center gap-2 font-medium">
              <TriangleAlert size={15} aria-hidden />
              {t("overview.connectionLost")}
            </span>
            <Button variant="outline" size="sm" onClick={() => void dashboard.reload()}>
              {t("overview.retry")}
            </Button>
          </div>
        </div>
      )}

      <AlertStack />

      <main className="w-full space-y-6 p-4 sm:p-6">
          <div className="flex items-center justify-between gap-3 md:hidden">
            <KiroLbWordmark />
            <SignOutButton onSignOut={signOut} />
          </div>
          <TabsList aria-label={t("navigation")} className="h-10! w-full flex-row! md:hidden">
            {navItems.map(({ value, label, icon: Icon }) => (
              <TabsTrigger key={value} value={value} className="w-auto! justify-center! gap-2 px-2 sm:px-3" title={label}>
                <Icon aria-hidden />
                <span className="hidden sm:inline">{label}</span>
              </TabsTrigger>
            ))}
          </TabsList>

          <TabsContent value="overview" className="space-y-6">
            <section className="grid grid-cols-2 gap-px overflow-hidden rounded-xl border bg-border shadow-sm sm:grid-cols-3 xl:grid-cols-6">
              {isLoading || !overview ? (
                Array.from({ length: 6 }).map((_, index) => <StatCardSkeleton key={index} />)
              ) : (
                <>
                  <StatCard
                    label={t("overview.totalTokens")} icon={<Coins />}
                    value={<span title={exactTokens(totals.totalTokens)}>{formatTokens(totals.totalTokens)}</span>}
                  />
                  <StatCard label={t("overview.requests24h")} icon={<Activity />} value={overview.requests24h.toLocaleString()} />
                  <StatCard
                    label={t("overview.success24h")} icon={<ShieldCheck />}
                    value={
                      <span
                        className={kpis?.success.isCritical ? "text-destructive" : undefined}
                        title={t("overview.successTitle", { ok: overview.successes24h.toLocaleString(), total: overview.requests24h.toLocaleString() })}
                      >
                        {kpis?.success.label.split(" (")[0]}
                      </span>
                    }
                  />
                  <StatCard
                    label={t("overview.routableAccounts")} icon={<ServerCog />}
                    value={
                      <span
                        className={kpis?.routableAccounts.isCritical ? "text-destructive" : undefined}
                        title={t("overview.routableTitle", { n: kpis?.routableAccounts.count ?? 0, total: kpis?.routableAccounts.total ?? 0 })}
                      >
                        {kpis?.routableAccounts.count}/{kpis?.routableAccounts.total}
                      </span>
                    }
                  />
                  <StatCard
                    label={t("overview.creditsUsed")} icon={<CreditCard />}
                    value={<span title={t("overview.creditsAccounts", { n: credits.accounts })}>{formatCreditTotal(credits.used)}</span>}
                  />
                  <StatCard
                    label={t("overview.creditsAvailable")} icon={<Wallet />}
                    value={
                      <span title={t("overview.creditsAccounts", { n: credits.accounts })}>
                        {formatCreditTotal(credits.available)} / {formatCreditTotal(credits.limit)}
                      </span>
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
              <TotalRateChart
                rate={dashboard.rate}
                isLoading={isLoading}
                rateWindow={dashboard.rateWindow}
                onWindowChange={dashboard.setRateWindow}
              />
              <TokenUsagePanel keyUsage={dashboard.keyUsage} isLoading={isLoading} />
            </section>

            <RequestLogTable
              page={dashboard.logs}
              isLoading={isLoading || dashboard.isLogsLoading}
              model={dashboard.logModel}
              order={dashboard.logOrder}
              onLimitChange={dashboard.setLogLimit}
              onOffsetChange={dashboard.setLogOffset}
              onModelChange={dashboard.setLogModel}
              onOrderChange={dashboard.setLogOrder}
            />
          </TabsContent>

          <TabsContent value="accounts" className="space-y-6">
            <AccountsPanel
              accounts={dashboard.accounts}
              tokenHubDashboardUrl={dashboard.overview?.tokenHubDashboardUrl}
              isLoading={isLoading}
              isMutating={isMutating}
              onDeleteAccount={(id) => void runAction(() => dashboardApi.deleteAccount(id))}
              onToggleAccount={(id, enabled) => void runAction(() => dashboardApi.setAccountEnabled(id, enabled))}
            />
            {/* Placed here rather than on Overview for the reason stated above:
                Overview stays pool-wide, and this is a per-account breakdown. It
                pairs with the quota column in the panel above - that one is what
                Kiro counts, this one is what the gateway measured. */}
            <AccountTokenPanel accountTokenUsage={dashboard.accountTokenUsage} rate={dashboard.rate} isLoading={isLoading} />
            <DeviceLoginCard onRegistered={dashboard.reload} />
          </TabsContent>

          <TabsContent value="keys">
            <ApiKeysPanel
              apiKeys={dashboard.apiKeys}
              keyUsage={dashboard.keyUsage}
              isLoading={isLoading}
              isMutating={isMutating}
              onCreate={() => setIsCreateKeyOpen(true)}
              onDelete={(id) => void runAction(() => dashboardApi.deleteApiKey(id))}
              onRename={(id, name) => void runAction(() => dashboardApi.renameApiKey(id, name))}
            />
          </TabsContent>

          <TabsContent value="info">
            <InfoPanel
              overview={overview}
              accounts={dashboard.accounts}
              routableAccounts={kpis?.routableAccounts?.count}
              lastUpdatedAt={dashboard.lastUpdatedAt}
              isCheckingUpdates={dashboard.isCheckingUpdates}
              onCheckUpdates={() => void dashboard.checkForUpdates()}
              isInstallingUpdate={dashboard.isInstallingUpdate}
              onInstallUpdate={(version) => void dashboard.installUpdate(version)}
            />
          </TabsContent>

          <TabsContent value="settings">
            <SettingsPanel onNotice={dashboard.notify} leading={<AppearancePanel />} />
          </TabsContent>
      </main>
      </div>

      <CreateKeyDialog open={isCreateKeyOpen} onOpenChange={setIsCreateKeyOpen} onCreate={createKey} />
    </Tabs>
  );
}
