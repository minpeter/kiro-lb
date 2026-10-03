import { type ReactNode, useCallback, useEffect, useState } from "react";
import {
  Check,
  Coins,
  Database,
  Gauge,
  Loader2,
  GripVertical,
  Network,
  PlugZap,
  ScrollText,
  TriangleAlert,
  Users,
  Waves,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { dashboardApi } from "../api";
import type {
  EndpointStrategy,
  EndpointPingResult,
  EndpointTestResult,
  DataOverview,
  EndpointOption,
  EndpointPingResponse,
  EndpointTestResponse,
  EndpointsResponse,
  GatewayTunables,
  ModelCostRow,
  PromptFilterSettings,
  ProxyChain,
} from "../types";
import { usePreferences } from "../preferences";
import { compareModels } from "../model-family";
import { ModelMark } from "./model-marks";
import { ModelListingCard } from "./model-listing-card";
import { TierRoutingCard } from "./tier-routing-card";
import { pushError } from "../alerts";
import { describeLatency, describeShortenStats, loadBalancingHelp, loadBalancingLabel } from "./routing-labels";


type BusyKind =
  | "save"
  | "test"
  | "ping"
  | "prompt"
  | "tunables"
  | "clear-text"
  | "clear-logs"
  | "clear-usage"
  | "proxies";

// Radix Select reserves the empty string, so the "omit" choice needs a stand-in.

/** Moves a key to an absolute position, so the first row can be dragged down. */
function reorder(order: string[], key: string, targetIndex: number): string[] {
  const from = order.indexOf(key);
  if (from < 0 || targetIndex < 0 || targetIndex >= order.length || from === targetIndex) return order;
  const next = [...order];
  next.splice(from, 1);
  next.splice(targetIndex, 0, key);
  return next;
}

export type SettingsPanelProps = {
  /** Raises a success message into the app-wide notice banner. */
  onNotice: (message: string) => void;
  leading?: ReactNode;
};

const AUTO_PROBE_MODEL = "__auto__";

/** The gateway accepts 0 (off) or 5..1440 minutes; anything between snaps to the minimum. */
function probeIntervalToSave(minutes: number): number {
  if (!Number.isFinite(minutes) || minutes <= 0) return 0;
  return Math.min(1440, Math.max(5, Math.round(minutes)));
}

export function SettingsPanel({ onNotice, leading }: SettingsPanelProps) {
  const { t } = usePreferences();
  const [endpoints, setEndpoints] = useState<EndpointsResponse | null>(null);
  const [promptFilter, setPromptFilter] = useState<PromptFilterSettings | null>(null);
  const [order, setOrder] = useState<string[]>([]);
  const [rotation, setRotation] = useState(false);
  const [cooldown, setCooldown] = useState(30);
  const [strategy, setStrategy] = useState<EndpointStrategy>("ordered");
  const [probeModel, setProbeModel] = useState("");
  const [probeInterval, setProbeInterval] = useState(0);
  const [modelIds, setModelIds] = useState<string[]>([]);
  const [busy, setBusy] = useState<BusyKind | null>(null);
  const [testResult, setTestResult] = useState<EndpointTestResponse | null>(null);
  const [pingResult, setPingResult] = useState<EndpointPingResponse | null>(null);
  const [reps, setReps] = useState(1);
  const [dragKey, setDragKey] = useState<string | null>(null);
  const [dropIndex, setDropIndex] = useState<number | null>(null);
  const [tunables, setTunables] = useState<GatewayTunables | null>(null);
  const [refreshSeconds, setRefreshSeconds] = useState(960);
  const [data, setData] = useState<DataOverview | null>(null);
  const [costs, setCosts] = useState<ModelCostRow[]>([]);
  const sortedCosts = [...costs].sort((a, b) => compareModels(a.model, b.model));
  const [proxies, setProxies] = useState<ProxyChain | null>(null);
  const [proxyText, setProxyText] = useState("");
  const [maxConcurrency, setMaxConcurrency] = useState(0);
  const [maxAccountConcurrency, setMaxAccountConcurrency] = useState(0);
  const [queueTimeout, setQueueTimeout] = useState(30);
  const [checking, setChecking] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [endpointData, promptData, tunableData, dataData, costData, proxyData] = await Promise.all([
        dashboardApi.endpoints(),
        dashboardApi.promptFilter(),
        dashboardApi.tunables(),
        dashboardApi.dataOverview(),
        dashboardApi.modelCosts(),
        dashboardApi.proxies(),
      ]);
      setEndpoints(endpointData);
      setPromptFilter(promptData);
      setTunables(tunableData);
      setRefreshSeconds(tunableData.tokenRefreshSeconds);
      setMaxConcurrency(tunableData.maxConcurrency);
      setMaxAccountConcurrency(tunableData.maxAccountConcurrency);
      setQueueTimeout(tunableData.queueTimeoutSeconds);
      setData(dataData);
      setCosts(costData.models);
      setProxies(proxyData);
      // The stored chain is masked, so it is shown but not edited in place:
      // resubmitting a masked password would send literal asterisks upstream.
      setProxyText(proxyData.proxies.map((entry) => entry.url).join("\n"));
      setOrder(endpointData.settings.order);
      setRotation(endpointData.settings.rotation);
      setCooldown(endpointData.settings.cooldownSeconds);
      setStrategy(endpointData.settings.strategy ?? "ordered");
      setProbeModel(endpointData.settings.probeModel ?? "");
      setProbeInterval(endpointData.settings.probeIntervalMinutes ?? 0);
      dashboardApi
        .dashboardModels()
        .then((data) => setModelIds(data.models.map((m) => m.id)))
        .catch(() => setModelIds([]));
      setReps(endpointData.pingRepsDefault);
    } catch (loadError) {
      pushError(loadError);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const run = async (kind: BusyKind, action: () => Promise<void>) => {
    setBusy(kind);
    try {
      await action();
    } catch (actionError) {
      pushError(actionError);
    } finally {
      setBusy(null);
    }
  };

  const save = () =>
    run("save", async () => {
      const saved = await dashboardApi.saveEndpoints({
        rotation,
        order,
        cooldownSeconds: cooldown,
        strategy,
        probeModel,
        probeIntervalMinutes: probeIntervalToSave(probeInterval),
      });
      setOrder(saved.settings.order);
      setEndpoints((previous) => (previous ? { ...previous, settings: saved.settings } : previous));
      onNotice(t("settings.savedNotice"));
    });

  const test = () =>
    run("test", async () => {
      // One call per provider so the UI can name the one in flight; the backend
      // lock would reject overlapping probes anyway.
      const merged: EndpointTestResult[] = [];
      setTestResult({ model: "", requestsSpent: 0, results: [] });
      setPingResult(null);
      try {
        for (const endpoint of available) {
          setChecking(endpoint.key);
          const partial = await dashboardApi.testEndpoints(endpoint.key);
          merged.push(...partial.results);
          setTestResult({ model: partial.model, requestsSpent: merged.length, results: [...merged] });
        }
      } finally {
        setChecking(null);
      }
    });

  const ping = () =>
    run("ping", async () => {
      const merged: EndpointPingResult[] = [];
      let last: EndpointPingResponse | null = null;
      setPingResult(null);
      setTestResult(null);
      try {
        for (const endpoint of available) {
          setChecking(endpoint.key);
          const partial = await dashboardApi.pingEndpoints(reps, endpoint.key);
          merged.push(...partial.results);
          last = partial;
          setPingResult({ ...partial, results: [...merged], requestsSpent: merged.length * reps });
        }
      } finally {
        setChecking(null);
      }
      // The verdict must compare every provider, so it is recomputed here from
      // the merged samples rather than taken from the last single-provider call.
      if (last && merged.length > 1) {
        const usable = merged.filter((row) => row.medianMs !== null);
        if (usable.length > 1) {
          const medians = usable.map((row) => row.medianMs as number);
          const between = Math.max(...medians) - Math.min(...medians);
          const within = Math.max(
            ...usable.map((row) => (row.maxMs ?? 0) - (row.minMs ?? 0)),
          );
          const fastest = usable.reduce((best, row) =>
            (row.medianMs as number) < (best.medianMs as number) ? row : best,
          );
          const conclusive = between > within;
          setPingResult({
            ...last,
            results: [...merged],
            requestsSpent: merged.length * reps,
            fastest: fastest.key,
            conclusive,
            betweenSpreadMs: Math.round(between),
            withinSpreadMs: Math.round(within),
            verdict: conclusive
              ? t("settings.verdictFastest", { name: fastest.name, between: Math.round(between), within: Math.round(within) })
              : t("settings.verdictIndistinguishable", { between: Math.round(between), within: Math.round(within) }),
          });
        }
      }
    });

  const saveTunables = (
    patch: Partial<
      Pick<
        GatewayTunables,
        | "tokenRefreshSeconds"
        | "loadBalancing"
        | "maxConcurrency"
        | "maxAccountConcurrency"
        | "queueTimeoutSeconds"
      >
    >,
  ) =>
    run("tunables", async () => {
      const saved = await dashboardApi.saveTunables(patch);
      setTunables(saved);
      setRefreshSeconds(saved.tokenRefreshSeconds);
      setData(await dashboardApi.dataOverview());
    });

  const saveProxies = () =>
    run("proxies", async () => {
      const entries = proxyText
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean);
      await dashboardApi.saveProxies(entries);
      const fresh = await dashboardApi.proxies();
      setProxies(fresh);
      setProxyText(fresh.proxies.map((entry) => entry.url).join("\n"));
      onNotice(entries.length ? t("settings.chainSaved", { n: entries.length }) : t("settings.chainCleared"));
    });

  const clear = (scope: "logs" | "usage") =>
    run(scope === "usage" ? "clear-usage" : "clear-logs", async () => {
      const result = await dashboardApi.clearData(scope);
      setData(await dashboardApi.dataOverview());
      onNotice(
        scope === "usage"
          ? t("settings.clearedUsage", { n: result.affected })
          : t("settings.deletedLogs", { n: result.affected }),
      );
    });

  const togglePrompt = (patch: { shortenTools?: boolean; writeHint?: boolean }) =>
    run("prompt", async () => {
      const saved = await dashboardApi.savePromptFilter(patch);
      setPromptFilter((previous) => (previous ? { ...previous, ...saved } : saved));
    });

  const savedEndpoints = endpoints?.settings;
  const endpointsDirty =
    !!savedEndpoints &&
    (rotation !== savedEndpoints.rotation ||
      order.join(",") !== savedEndpoints.order.join(",") ||
      cooldown !== savedEndpoints.cooldownSeconds ||
      strategy !== (savedEndpoints.strategy ?? "ordered") ||
      probeModel !== (savedEndpoints.probeModel ?? "") ||
      probeInterval !== (savedEndpoints.probeIntervalMinutes ?? 0));
  const savedProxyText = (proxies?.proxies ?? []).map((entry) => entry.url).join("\n");
  const proxiesDirty =
    !!proxies &&
    proxyText
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean)
      .join("\n") !== savedProxyText;
  const limitsDirty =
    !!tunables &&
    (maxConcurrency !== tunables.maxConcurrency ||
      maxAccountConcurrency !== tunables.maxAccountConcurrency ||
      queueTimeout !== tunables.queueTimeoutSeconds);

  const available = endpoints?.available ?? [];
  const isBusy = busy !== null;
  const pingCost = reps * Math.max(available.length, 1);

  // Active providers first, in attempt order, so the list reads as the priority
  // it represents; disabled ones sit below.
  const byKey = new Map(available.map((endpoint) => [endpoint.key, endpoint]));
  const rows = [
    ...order.map((key) => byKey.get(key)).filter((endpoint): endpoint is EndpointOption => Boolean(endpoint)),
    ...available.filter((endpoint) => !order.includes(endpoint.key)),
  ];

  return (
    <div className="space-y-6">
      <div className="grid gap-6 xl:grid-cols-2">
        {leading}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <PlugZap size={16} aria-hidden /> {t("settings.providersTitle")}
            </CardTitle>
            <CardDescription>{t("settings.providersDescription")}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={rotation}
                disabled={isBusy}
                onChange={(event) => setRotation(event.target.checked)}
              />
              {t("settings.rotate")}
            </label>
            {!rotation && (
              <p className="text-xs text-muted-foreground">
                {t("settings.rotationOff")}
              </p>
            )}

            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t("settings.provider")}</TableHead>
                  <TableHead>{t("settings.url")}</TableHead>
                  <TableHead className="text-right">{t("settings.enabled")}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((endpoint) => {
                  const position = order.indexOf(endpoint.key);
                  const active = position >= 0;
                  const lastActive = active && order.length === 1;
                  const isDragging = dragKey === endpoint.key;
                  const isTarget = dropIndex === position && dragKey !== null && !isDragging;
                  return (
                    <TableRow
                      key={endpoint.key}
                      draggable={active && !isBusy}
                      aria-grabbed={isDragging || undefined}
                      onDragStart={() => setDragKey(endpoint.key)}
                      onDragEnd={() => {
                        setDragKey(null);
                        setDropIndex(null);
                      }}
                      onDragOver={(event) => {
                        if (!active || dragKey === null) return;
                        event.preventDefault();
                        setDropIndex(position);
                      }}
                      onDrop={(event) => {
                        event.preventDefault();
                        if (dragKey === null || !active) return;
                        setOrder((previous) => reorder(previous, dragKey, position));
                        setDragKey(null);
                        setDropIndex(null);
                      }}
                      className={[
                        active && !isBusy ? "cursor-grab" : "",
                        isDragging ? "opacity-50" : "",
                        isTarget ? "border-t-2 border-t-primary" : "",
                      ]
                        .filter(Boolean)
                        .join(" ")}
                    >
                      <TableCell>
                        <div className="flex items-center gap-2">
                          {active ? (
                            <span
                              role="button"
                              tabIndex={isBusy ? -1 : 0}
                              aria-label={t("settings.reorderAria", { name: endpoint.name, position: position + 1, total: order.length })}
                              title={t("settings.reorderTitle")}
                              className="text-muted-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring rounded"
                              onKeyDown={(event) => {
                                if (isBusy) return;
                                const delta = event.key === "ArrowUp" ? -1 : event.key === "ArrowDown" ? 1 : 0;
                                if (delta === 0) return;
                                event.preventDefault();
                                setOrder((previous) => reorder(previous, endpoint.key, position + delta));
                              }}
                            >
                              <GripVertical size={14} aria-hidden />
                            </span>
                          ) : (
                            <span className="w-[14px]" />
                          )}
                          <span className="font-medium">{endpoint.name}</span>
                          {active && <Badge variant="secondary">#{position + 1}</Badge>}
                        </div>
                      </TableCell>
                      <TableCell className="font-mono text-xs text-muted-foreground">{endpoint.url}</TableCell>
                      <TableCell className="text-right">
                        <input
                          type="checkbox"
                          checked={active}
                          disabled={isBusy || lastActive}
                          aria-label={t("settings.enableAria", { name: endpoint.name })}
                          title={lastActive ? t("settings.lastProvider") : undefined}
                          onChange={(event) =>
                            setOrder((previous) =>
                              event.target.checked
                                ? [...previous, endpoint.key]
                                : previous.filter((key) => key !== endpoint.key),
                            )
                          }
                        />
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
            {strategy === "fastest" &&
              (Object.keys(endpoints?.latency?.medians ?? {}).length === 0 ? (
                <p className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning">
                  <TriangleAlert size={14} className="mt-0.5 shrink-0" aria-hidden />
                  <span>{describeLatency(endpoints?.latency, endpoints?.region, t)}</span>
                </p>
              ) : (
                <p className="text-xs text-muted-foreground">{describeLatency(endpoints?.latency, endpoints?.region, t)}</p>
              ))}

            <div className="flex flex-wrap items-end gap-4">
              <div className="inline-grid gap-1">
                <Label htmlFor="cooldown" className="whitespace-nowrap">{t("settings.cooldown")}</Label>
                <Input
                  id="cooldown"
                  type="number"
                  min={0}
                  max={3600}
                  value={cooldown}
                  disabled={isBusy}
                  className="w-0 min-w-full"
                  onChange={(event) => setCooldown(Number(event.target.value))}
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="endpoint-strategy">{t("settings.endpointStrategy")}</Label>
                <Select value={strategy} disabled={isBusy} onValueChange={(value) => setStrategy(value as EndpointStrategy)}>
                  <SelectTrigger id="endpoint-strategy" className="w-44">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="ordered">{t("settings.strategyOrdered")}</SelectItem>
                    <SelectItem value="fastest">{t("settings.strategyFastest")}</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              {strategy === "fastest" && (
                <>
                  <div className="space-y-1">
                    <Label htmlFor="probe-model">{t("settings.probeModel")}</Label>
                    <Select
                      value={probeModel || AUTO_PROBE_MODEL}
                      disabled={isBusy}
                      onValueChange={(value) => setProbeModel(value === AUTO_PROBE_MODEL ? "" : value)}
                    >
                      <SelectTrigger id="probe-model" className="w-56">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value={AUTO_PROBE_MODEL}>{t("settings.probeModelAuto")}</SelectItem>
                        {modelIds.map((id) => (
                          <SelectItem key={id} value={id}>
                            {id}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="inline-grid gap-1">
                    <Label htmlFor="probe-interval" className="whitespace-nowrap">{t("settings.probeInterval")}</Label>
                    <Input
                      id="probe-interval"
                      type="number"
                      min={0}
                      max={endpoints?.probeIntervalRange?.[1] ?? 1440}
                      value={probeInterval}
                      disabled={isBusy}
                      className="w-0 min-w-full"
                      onChange={(event) => setProbeInterval(Number(event.target.value))}
                      onBlur={() => setProbeInterval((value) => probeIntervalToSave(value))}
                    />
                  </div>
                </>
              )}
              <Button onClick={save} disabled={isBusy || order.length === 0 || !endpointsDirty}>
                {busy === "save" ? t("settings.saving") : t("settings.save")}
              </Button>
            </div>
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-6 xl:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Gauge size={16} aria-hidden /> {t("settings.connectivityTitle")}
            </CardTitle>
            <CardDescription className="flex items-start gap-2">
              <TriangleAlert size={14} className="mt-0.5 shrink-0" aria-hidden />
              <span>{t("settings.connectivityDescription", { n: pingCost })}</span>
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex flex-wrap items-end gap-4">
              <Button variant="secondary" onClick={test} disabled={isBusy}>
                {busy === "test" ? t("settings.testing") : t("settings.testAll")}
              </Button>
              <Button variant="secondary" onClick={ping} disabled={isBusy}>
                {busy === "ping" ? t("settings.measuring") : t("settings.ping")}
              </Button>
            </div>

            {(busy === "test" || busy === "ping") && (
              <div className="space-y-1">
                {available.map((endpoint) => {
                  const done =
                    (testResult?.results ?? []).some((row) => row.key === endpoint.key) ||
                    (pingResult?.results ?? []).some((row) => row.key === endpoint.key);
                  const active = checking === endpoint.key;
                  return (
                    <div key={endpoint.key} className="flex items-center gap-2 text-sm">
                      {active ? (
                        <Loader2 size={14} className="animate-spin text-muted-foreground" aria-hidden />
                      ) : done ? (
                        <Check size={14} className="text-success" aria-hidden />
                      ) : (
                        <span className="inline-block size-[14px]" />
                      )}
                      <span className={active ? "font-medium" : done ? "" : "text-muted-foreground"}>
                        {endpoint.name}
                      </span>
                      {active && <span className="text-xs text-muted-foreground">{t("settings.checking")}</span>}
                    </div>
                  );
                })}
              </div>
            )}

            {testResult && (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>{t("settings.provider")}</TableHead>
                    <TableHead>{t("settings.result")}</TableHead>
                    <TableHead className="text-right">{t("settings.firstByte")}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {testResult.results.map((row) => (
                    <TableRow key={row.key}>
                      <TableCell className="font-medium">{row.name}</TableCell>
                      <TableCell>
                        {row.ok ? (
                          <Badge variant="secondary">{t("settings.accepted")}</Badge>
                        ) : (
                          <span className="text-destructive">{row.error ?? t("settings.failed")}</span>
                        )}
                      </TableCell>
                      <TableCell className="text-right">{row.ttfbMs === null ? "—" : `${row.ttfbMs} ms`}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}

            {pingResult && (
              <div className="space-y-3">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>{t("settings.provider")}</TableHead>
                      <TableHead className="text-right">{t("settings.samples")}</TableHead>
                      <TableHead className="text-right">{t("settings.median")}</TableHead>
                      <TableHead className="text-right">{t("settings.min")}</TableHead>
                      <TableHead className="text-right">{t("settings.max")}</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {pingResult.results.map((row) => (
                      <TableRow key={row.key}>
                        <TableCell className="font-medium">
                          {row.name}
                          {pingResult.conclusive && pingResult.fastest === row.key && (
                            <Badge variant="secondary" className="ml-2">
                              {t("settings.fastest")}
                            </Badge>
                          )}
                        </TableCell>
                        <TableCell className="text-right">{row.samples}</TableCell>
                        <TableCell className="text-right">{row.medianMs === null ? "—" : `${row.medianMs} ms`}</TableCell>
                        <TableCell className="text-right">{row.minMs === null ? "—" : `${row.minMs} ms`}</TableCell>
                        <TableCell className="text-right">{row.maxMs === null ? "—" : `${row.maxMs} ms`}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
                <p className={pingResult.conclusive ? "text-sm" : "text-sm text-muted-foreground"}>
                  {pingResult.verdict}
                </p>
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Users size={16} aria-hidden /> {t("settings.poolTitle")}
            </CardTitle>
            <CardDescription>{t("settings.poolDescription")}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-1">
              <Label htmlFor="balancing">{t("settings.accountOrdering")}</Label>
              <Select
                value={tunables?.loadBalancing}
                disabled={isBusy || !tunables}
                onValueChange={(value) => saveTunables({ loadBalancing: value })}
              >
                <SelectTrigger id="balancing" className="w-80">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {(tunables?.loadBalancingOptions ?? []).map((option) => (
                    <SelectItem key={option} value={option}>
                      {loadBalancingLabel(option, t)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {loadBalancingHelp(tunables?.loadBalancing, t) && (
                <p className="text-xs text-muted-foreground">{loadBalancingHelp(tunables?.loadBalancing, t)}</p>
              )}
            </div>

            <div className="flex flex-wrap items-end gap-4">
              <div className="inline-grid gap-1">
                <Label htmlFor="refresh" className="whitespace-nowrap">{t("settings.refreshBefore")}</Label>
                <Input
                  id="refresh"
                  type="number"
                  min={60}
                  max={1800}
                  value={refreshSeconds}
                  disabled={isBusy || !tunables}
                  className="w-0 min-w-full"
                  onChange={(event) => setRefreshSeconds(Number(event.target.value))}
                />
              </div>
              <Button
                disabled={isBusy || !tunables || refreshSeconds === tunables?.tokenRefreshSeconds}
                onClick={() => saveTunables({ tokenRefreshSeconds: refreshSeconds })}
              >
                {t("settings.save")}
              </Button>
            </div>
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-6 xl:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Database size={16} aria-hidden /> {t("settings.dataTitle")}
            </CardTitle>
            <CardDescription>{t("settings.dataDescription")}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {data && (
              <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
                <Field label={t("settings.requestLogs")} value={data.requestLogs.toLocaleString()} />
                <Field label={t("settings.retention")} value={t("settings.retentionDays", { n: data.retentionDays })} />
                <Field label={t("settings.database")} value={`${(data.databaseBytes / 1048576).toFixed(1)} MB`} />
              </div>
            )}

            <div className="flex flex-wrap gap-2">
              <Button variant="secondary" disabled={isBusy} onClick={() => clear("usage")}>
                {busy === "clear-usage" ? t("settings.clearing") : t("settings.clearUsage")}
              </Button>
              <Button
                variant="destructive"
                disabled={isBusy || !data || data.requestLogs === 0}
                onClick={() => clear("logs")}
              >
                {busy === "clear-logs" ? t("settings.clearing") : t("settings.clearLogs")}
              </Button>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <ScrollText size={16} aria-hidden /> {t("settings.promptTitle")}
            </CardTitle>
            <CardDescription>{t("settings.shortenNote")}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={promptFilter?.shortenTools ?? false}
                disabled={isBusy || !promptFilter || promptFilter.shortenTools === undefined}
                onChange={(event) => togglePrompt({ shortenTools: event.target.checked })}
              />
              {t("settings.shortenTools", { n: promptFilter?.shortenThreshold ?? 1200 })}
            </label>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={promptFilter?.writeHint ?? false}
                disabled={isBusy || !promptFilter || promptFilter.writeHint === undefined}
                onChange={(event) => togglePrompt({ writeHint: event.target.checked })}
              />
              {t("settings.writeHint")}
            </label>
            {describeShortenStats(promptFilter?.lastShorten, t) && (
              <p className="text-xs text-muted-foreground">{describeShortenStats(promptFilter?.lastShorten, t)}</p>
            )}
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-6 xl:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Network size={16} aria-hidden /> {t("settings.proxiesTitle")}
            </CardTitle>
            <CardDescription>
              {t("settings.proxiesDescription", { n: proxies?.cooldownSeconds ?? 60, schemes: (proxies?.schemes ?? []).join(", ") })}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <textarea
              className="min-h-24 w-full rounded-md border border-input bg-transparent p-3 font-mono text-xs outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50 disabled:opacity-50"
              spellCheck={false}
              placeholder={"socks5h://user:pass@host:1080\nhttp://backup:8080"}
              value={proxyText}
              disabled={isBusy}
              onChange={(event) => setProxyText(event.target.value)}
            />
            {proxies && proxies.proxies.length > 0 && (
              <div className="space-y-1">
                {proxies.proxies.map((entry, index) => (
                  <div key={entry.url} className="flex items-center gap-2 text-xs">
                    <Badge variant="secondary">#{index + 1}</Badge>
                    <span className="font-mono">{entry.url}</span>
                    {entry.cooling && <span className="text-destructive">{t("settings.coolingDown")}</span>}
                  </div>
                ))}
              </div>
            )}
            <Button onClick={saveProxies} disabled={isBusy || !proxiesDirty}>
              {busy === "proxies" ? t("settings.saving") : t("settings.saveProxies")}
            </Button>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Waves size={16} aria-hidden /> {t("settings.concurrencyTitle")}
            </CardTitle>
            <CardDescription>{t("settings.concurrencyDescription")}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid gap-4 sm:grid-cols-3">
              <div className="space-y-1">
                <Label htmlFor="max-conc">{t("settings.totalInFlight")}</Label>
                <Input
                  id="max-conc"
                  type="number"
                  min={0}
                  max={512}
                  value={maxConcurrency}
                  disabled={isBusy}
                  onChange={(event) => setMaxConcurrency(Number(event.target.value))}
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="max-acct">{t("settings.perAccount")}</Label>
                <Input
                  id="max-acct"
                  type="number"
                  min={0}
                  max={128}
                  value={maxAccountConcurrency}
                  disabled={isBusy}
                  onChange={(event) => setMaxAccountConcurrency(Number(event.target.value))}
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="queue-timeout">{t("settings.queueWait")}</Label>
                <Input
                  id="queue-timeout"
                  type="number"
                  min={1}
                  max={600}
                  value={queueTimeout}
                  disabled={isBusy}
                  onChange={(event) => setQueueTimeout(Number(event.target.value))}
                />
              </div>
            </div>
            <p className="text-xs text-muted-foreground">
              {t("settings.queueNote")}
            </p>
            <Button
              disabled={isBusy || !limitsDirty}
              onClick={() =>
                saveTunables({
                  maxConcurrency,
                  maxAccountConcurrency,
                  queueTimeoutSeconds: queueTimeout,
                })
              }
            >
              {busy === "tunables" ? t("settings.saving") : t("settings.saveLimits")}
            </Button>
          </CardContent>
        </Card>
      </div>

      <TierRoutingCard onNotice={onNotice} />

      <ModelListingCard onNotice={onNotice} />

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Coins size={16} aria-hidden /> {t("settings.costTitle")}
          </CardTitle>
          <CardDescription>{t("settings.costNote")}</CardDescription>
        </CardHeader>
        <CardContent>
          {/* 19 rows is a tall column on its own; split it once there is room. */}
          <div className="grid gap-x-8 lg:grid-cols-2">
            {[sortedCosts.slice(0, Math.ceil(sortedCosts.length / 2)), sortedCosts.slice(Math.ceil(sortedCosts.length / 2))].map(
              (half, index) => (
                <Table key={index}>
                  <TableHeader>
                    <TableRow>
                      <TableHead>{t("settings.model")}</TableHead>
                      <TableHead className="text-right">{t("settings.multiplier")}</TableHead>
                      <TableHead className="text-right">{t("settings.context")}</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {half.map((row) => (
                      <TableRow key={row.model}>
                        <TableCell className="font-medium">
                          <span className="flex items-center gap-2">
                            <ModelMark model={row.model} />
                            {row.model}
                          </span>
                        </TableCell>
                        <TableCell className="text-right tabular-nums">
                          {row.multiplier}x
                          {row.longMultiplier !== null && row.longThresholdTokens !== null && (
                            <span className="text-muted-foreground">
                              {" "}
                              · {row.longMultiplier}x {t("settings.above")}{" "}
                              {(row.longThresholdTokens / 1000).toFixed(0)}K
                            </span>
                          )}
                        </TableCell>
                        <TableCell className="text-right tabular-nums text-muted-foreground">
                          {row.contextTokens ? `${(row.contextTokens / 1000).toFixed(0)}K` : "—"}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              ),
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="space-y-1">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="text-sm tabular-nums">{value}</p>
    </div>
  );
}
