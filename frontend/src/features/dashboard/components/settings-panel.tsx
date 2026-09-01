import { useCallback, useEffect, useState } from "react";
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
  Workflow,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { dashboardApi, DashboardApiError } from "../api";
import type {
  AgentModeSettings,
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

const describe = (error: unknown) =>
  error instanceof DashboardApiError ? error.message : "Unexpected error";

// Radix Select reserves the empty string, so the "omit" choice needs a stand-in.
type BusyKind =
  | "save"
  | "test"
  | "ping"
  | "prompt"
  | "mode"
  | "tunables"
  | "clear-text"
  | "clear-logs"
  | "clear-usage"
  | "proxies";

// Radix Select reserves the empty string, so the "omit" choice needs a stand-in.
const OMIT_VALUE = "__omit__";

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
};

export function SettingsPanel({ onNotice }: SettingsPanelProps) {
  const [endpoints, setEndpoints] = useState<EndpointsResponse | null>(null);
  const [promptFilter, setPromptFilter] = useState<PromptFilterSettings | null>(null);
  const [agentMode, setAgentMode] = useState<AgentModeSettings | null>(null);
  const [order, setOrder] = useState<string[]>([]);
  const [rotation, setRotation] = useState(false);
  const [cooldown, setCooldown] = useState(30);
  const [busy, setBusy] = useState<BusyKind | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<EndpointTestResponse | null>(null);
  const [pingResult, setPingResult] = useState<EndpointPingResponse | null>(null);
  const [reps, setReps] = useState(1);
  const [dragKey, setDragKey] = useState<string | null>(null);
  const [dropIndex, setDropIndex] = useState<number | null>(null);
  const [tunables, setTunables] = useState<GatewayTunables | null>(null);
  const [refreshSeconds, setRefreshSeconds] = useState(600);
  const [data, setData] = useState<DataOverview | null>(null);
  const [costs, setCosts] = useState<ModelCostRow[]>([]);
  const [costNote, setCostNote] = useState("");
  const [proxies, setProxies] = useState<ProxyChain | null>(null);
  const [proxyText, setProxyText] = useState("");
  const [maxConcurrency, setMaxConcurrency] = useState(0);
  const [maxAccountConcurrency, setMaxAccountConcurrency] = useState(0);
  const [queueTimeout, setQueueTimeout] = useState(30);
  const [checking, setChecking] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [endpointData, promptData, modeData, tunableData, dataData, costData, proxyData] = await Promise.all([
        dashboardApi.endpoints(),
        dashboardApi.promptFilter(),
        dashboardApi.agentMode(),
        dashboardApi.tunables(),
        dashboardApi.dataOverview(),
        dashboardApi.modelCosts(),
        dashboardApi.proxies(),
      ]);
      setEndpoints(endpointData);
      setPromptFilter(promptData);
      setAgentMode(modeData);
      setTunables(tunableData);
      setRefreshSeconds(tunableData.tokenRefreshSeconds);
      setMaxConcurrency(tunableData.maxConcurrency);
      setMaxAccountConcurrency(tunableData.maxAccountConcurrency);
      setQueueTimeout(tunableData.queueTimeoutSeconds);
      setData(dataData);
      setCosts(costData.models);
      setCostNote(costData.note);
      setProxies(proxyData);
      // The stored chain is masked, so it is shown but not edited in place:
      // resubmitting a masked password would send literal asterisks upstream.
      setProxyText(proxyData.proxies.map((entry) => entry.url).join("\n"));
      setOrder(endpointData.settings.order);
      setRotation(endpointData.settings.rotation);
      setCooldown(endpointData.settings.cooldownSeconds);
      setReps(endpointData.pingRepsDefault);
    } catch (loadError) {
      setError(describe(loadError));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const run = async (kind: BusyKind, action: () => Promise<void>) => {
    setBusy(kind);
    setError(null);
    try {
      await action();
    } catch (actionError) {
      setError(describe(actionError));
    } finally {
      setBusy(null);
    }
  };

  const save = () =>
    run("save", async () => {
      const saved = await dashboardApi.saveEndpoints({ rotation, order, cooldownSeconds: cooldown });
      setOrder(saved.settings.order);
      onNotice("Saved. It applies to the next request, with no restart.");
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
              ? `${fastest.name} is fastest by ${Math.round(between)}ms, which exceeds the widest single-provider spread of ${Math.round(within)}ms.`
              : `Indistinguishable: the ${Math.round(between)}ms gap between providers is smaller than the ${Math.round(within)}ms spread within one. Raise repetitions for a firmer answer.`,
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
      onNotice(entries.length ? `Chain saved with ${entries.length} proxy(ies).` : "Chain cleared: direct connections.");
    });

  const clear = (scope: "logs" | "usage") =>
    run(scope === "usage" ? "clear-usage" : "clear-logs", async () => {
      const result = await dashboardApi.clearData(scope);
      setData(await dashboardApi.dataOverview());
      onNotice(
        scope === "usage"
          ? `Cleared ${result.affected} token usage row(s).`
          : `Deleted ${result.affected} request log(s).`,
      );
    });

  const saveMode = (next: string) =>
    run("mode", async () => {
      const saved = await dashboardApi.saveAgentMode(next);
      setAgentMode((previous) => (previous ? { ...previous, mode: saved.mode } : previous));
    });

  const togglePrompt = (next: boolean) =>
    run("prompt", async () => {
      await dashboardApi.savePromptFilter(next);
      setPromptFilter((previous) => (previous ? { ...previous, enabled: next } : previous));
    });

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
      {error && (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">
          {error}
        </div>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <PlugZap size={16} aria-hidden /> Generation providers
          </CardTitle>
          <CardDescription>
            Which upstreams may serve a generation request, and in what order. Only the runtime host is
            verified for every credential type; the alternates may reject some accounts, so test before
            relying on one.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={rotation}
              disabled={isBusy}
              onChange={(event) => setRotation(event.target.checked)}
            />
            Rotate to the next provider when one fails
          </label>
          {!rotation && (
            <p className="text-xs text-muted-foreground">
              With rotation off, only the account's own generation URL is used and the order below is ignored.
            </p>
          )}

          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Provider</TableHead>
                <TableHead>URL</TableHead>
                <TableHead className="text-right">Enabled</TableHead>
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
                            aria-label={`Reorder ${endpoint.name}, currently position ${position + 1} of ${order.length}. Use the arrow keys.`}
                            title="Drag to reorder, or focus and use the arrow keys"
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
                        aria-label={`Enable ${endpoint.name}`}
                        title={lastActive ? "At least one provider must stay enabled" : undefined}
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
          <p className="text-xs text-muted-foreground">
            Drag a row to set the attempt order, or focus its handle and use the arrow keys.
          </p>

          <div className="flex flex-wrap items-end gap-4">
            <div className="space-y-1">
              <Label htmlFor="cooldown">Cooldown after a failure (seconds)</Label>
              <Input
                id="cooldown"
                type="number"
                min={0}
                max={3600}
                value={cooldown}
                disabled={isBusy}
                className="w-32"
                onChange={(event) => setCooldown(Number(event.target.value))}
              />
            </div>
            <Button onClick={save} disabled={isBusy || order.length === 0}>
              {busy === "save" ? "Saving..." : "Save"}
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Gauge size={16} aria-hidden /> Connectivity and latency
          </CardTitle>
          <CardDescription className="flex items-start gap-2">
            <TriangleAlert size={14} className="mt-0.5 shrink-0" aria-hidden />
            <span>
              Both send real generation requests and spend quota. Test sends one per provider; ping sends
              {" "}
              {pingCost} in total.
            </span>
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex flex-wrap items-end gap-4">
            <Button variant="secondary" onClick={test} disabled={isBusy}>
              {busy === "test" ? "Testing..." : "Test all providers"}
            </Button>
            <div className="space-y-1">
              <Label htmlFor="reps">Ping repetitions</Label>
              <Input
                id="reps"
                type="number"
                min={1}
                max={endpoints?.pingRepsMax ?? 10}
                value={reps}
                disabled={isBusy}
                className="w-24"
                onChange={(event) => setReps(Number(event.target.value))}
              />
            </div>
            <Button variant="secondary" onClick={ping} disabled={isBusy}>
              {busy === "ping" ? "Measuring..." : "Ping"}
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
                    {active && <span className="text-xs text-muted-foreground">checking...</span>}
                  </div>
                );
              })}
            </div>
          )}

          {testResult && (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Provider</TableHead>
                  <TableHead>Result</TableHead>
                  <TableHead className="text-right">First byte</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {testResult.results.map((row) => (
                  <TableRow key={row.key}>
                    <TableCell className="font-medium">{row.name}</TableCell>
                    <TableCell>
                      {row.ok ? (
                        <Badge variant="secondary">accepted</Badge>
                      ) : (
                        <span className="text-destructive">{row.error ?? "failed"}</span>
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
                    <TableHead>Provider</TableHead>
                    <TableHead className="text-right">Samples</TableHead>
                    <TableHead className="text-right">Median</TableHead>
                    <TableHead className="text-right">Min</TableHead>
                    <TableHead className="text-right">Max</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {pingResult.results.map((row) => (
                    <TableRow key={row.key}>
                      <TableCell className="font-medium">
                        {row.name}
                        {pingResult.conclusive && pingResult.fastest === row.key && (
                          <Badge variant="secondary" className="ml-2">
                            fastest
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
            <Workflow size={16} aria-hidden /> Agent task mode
          </CardTitle>
          <CardDescription>
            Sent as <span className="font-mono">conversationState.agentTaskType</span>. The official Kiro CLI
            sends <span className="font-mono">vibe</span> for free-form chat and reserves{" "}
            <span className="font-mono">spec</span> and <span className="font-mono">task</span> for its
            structured modes. What each one changes upstream is undocumented, so "omit" reproduces the payload
            as it was before this option existed.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="space-y-1">
            <Label htmlFor="agent-mode">Mode</Label>
            <Select
              value={agentMode ? agentMode.mode || OMIT_VALUE : undefined}
              disabled={isBusy || !agentMode}
              onValueChange={(value) => saveMode(value === OMIT_VALUE ? "" : value)}
            >
              <SelectTrigger id="agent-mode" className="w-48">
                <SelectValue placeholder="Select a mode" />
              </SelectTrigger>
              <SelectContent>
                {(agentMode?.allowed ?? []).map((mode) => (
                  <SelectItem key={mode || OMIT_VALUE} value={mode || OMIT_VALUE}>
                    {mode === "" ? "omit the field" : mode}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Users size={16} aria-hidden /> Account pool
          </CardTitle>
          <CardDescription>
            How the next account is chosen, and how early tokens are refreshed. Disable individual accounts in
            the Accounts tab.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-1">
            <Label htmlFor="balancing">Account ordering</Label>
            <Select
              value={tunables?.loadBalancing}
              disabled={isBusy || !tunables}
              onValueChange={(value) => saveTunables({ loadBalancing: value })}
            >
              <SelectTrigger id="balancing" className="w-56">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {(tunables?.loadBalancingOptions ?? []).map((option) => (
                  <SelectItem key={option} value={option}>
                    {option === "weighted"
                      ? "weighted — random, by remaining quota"
                      : option === "most_credits"
                        ? "most credits first"
                        : "sticky — keep the current account"}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="flex flex-wrap items-end gap-4">
            <div className="space-y-1">
              <Label htmlFor="refresh">Refresh token this many seconds before expiry</Label>
              <Input
                id="refresh"
                type="number"
                min={60}
                max={3600}
                value={refreshSeconds}
                disabled={isBusy || !tunables}
                className="w-32"
                onChange={(event) => setRefreshSeconds(Number(event.target.value))}
              />
            </div>
            <Button
              variant="secondary"
              disabled={isBusy || !tunables || refreshSeconds === tunables?.tokenRefreshSeconds}
              onClick={() => saveTunables({ tokenRefreshSeconds: refreshSeconds })}
            >
              Save
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Database size={16} aria-hidden /> Data
          </CardTitle>
          <CardDescription>
            What the gateway keeps on disk. The request log holds metadata only; request and response text is
            never stored.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {data && (
            <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
              <Field label="Request logs" value={data.requestLogs.toLocaleString()} />
              <Field label="Retention" value={`${data.retentionDays} days`} />
              <Field label="Database" value={`${(data.databaseBytes / 1048576).toFixed(1)} MB`} />
            </div>
          )}

          <div className="flex flex-wrap gap-2">
            <Button variant="secondary" disabled={isBusy} onClick={() => clear("usage")}>
              {busy === "clear-usage" ? "Clearing..." : "Clear token usage"}
            </Button>
            <Button
              variant="destructive"
              disabled={isBusy || !data || data.requestLogs === 0}
              onClick={() => clear("logs")}
            >
              {busy === "clear-logs" ? "Clearing..." : "Clear all request logs"}
            </Button>
          </div>
          <p className="text-xs text-muted-foreground">
            Clearing logs also resets the request metrics derived from them.
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <ScrollText size={16} aria-hidden /> Claude Code prompt
          </CardTitle>
          <CardDescription>{promptFilter?.preservedNote}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={promptFilter?.enabled ?? false}
              disabled={isBusy || !promptFilter}
              onChange={(event) => togglePrompt(event.target.checked)}
            />
            Replace Anthropic's generic prompt sections with a short Kiro one
          </label>
          {promptFilter && (
            <p className="text-xs text-muted-foreground">
              Dropped sections: <span className="font-mono">{promptFilter.droppedSections.join(", ")}</span>
            </p>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Network size={16} aria-hidden /> Proxies
          </CardTitle>
          <CardDescription>
            One per line, in the order they should be tried. A proxy that fails a connection is moved to the
            back for {proxies?.cooldownSeconds ?? 60}s. Leave empty to connect directly. Passwords are masked
            everywhere they are shown.
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
          <p className="text-xs text-muted-foreground">
            Accepted: <span className="font-mono">{(proxies?.schemes ?? []).join(", ")}</span>. Use{" "}
            <span className="font-mono">socks5h</span> to resolve DNS at the proxy.
          </p>
          {proxies && proxies.proxies.length > 0 && (
            <div className="space-y-1">
              {proxies.proxies.map((entry, index) => (
                <div key={entry.url} className="flex items-center gap-2 text-xs">
                  <Badge variant="secondary">#{index + 1}</Badge>
                  <span className="font-mono">{entry.url}</span>
                  {entry.cooling && <span className="text-destructive">cooling down</span>}
                </div>
              ))}
            </div>
          )}
          <Button onClick={saveProxies} disabled={isBusy}>
            {busy === "proxies" ? "Saving..." : "Save proxies"}
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Waves size={16} aria-hidden /> Concurrency
          </CardTitle>
          <CardDescription>
            Caps how many generation requests run at once. 0 means no cap. Holding a burst here is cheaper
            than being rate limited upstream, which costs an account cooldown each time.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-3">
            <div className="space-y-1">
              <Label htmlFor="max-conc">Total in flight</Label>
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
              <Label htmlFor="max-acct">Per account</Label>
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
              <Label htmlFor="queue-timeout">Queue wait (seconds)</Label>
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
            A request that waits longer than the queue wait fails with 503 instead of hanging.
          </p>
          <Button
            variant="secondary"
            disabled={isBusy}
            onClick={() =>
              saveTunables({
                maxConcurrency,
                maxAccountConcurrency,
                queueTimeoutSeconds: queueTimeout,
              })
            }
          >
            {busy === "tunables" ? "Saving..." : "Save limits"}
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Coins size={16} aria-hidden /> Kiro credit cost
          </CardTitle>
          <CardDescription>{costNote}</CardDescription>
        </CardHeader>
        <CardContent>
          {/* 19 rows is a tall column on its own; split it once there is room. */}
          <div className="grid gap-x-8 lg:grid-cols-2">
            {[costs.slice(0, Math.ceil(costs.length / 2)), costs.slice(Math.ceil(costs.length / 2))].map(
              (half, index) => (
                <Table key={index}>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Model</TableHead>
                      <TableHead className="text-right">Multiplier</TableHead>
                      <TableHead className="text-right">Context</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {half.map((row) => (
                      <TableRow key={row.model}>
                        <TableCell className="font-medium">{row.model}</TableCell>
                        <TableCell className="text-right tabular-nums">{row.multiplier}x</TableCell>
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
