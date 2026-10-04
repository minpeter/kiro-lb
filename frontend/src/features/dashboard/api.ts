import type {
  DashboardModels,
  Account,
  AccountTokenUsage,
  ApiKey,
  BrowserLoginFlow,
  DataOverview,
  DeviceLoginFlow,
  DeviceLoginProvider,
  EndpointPingResponse,
  EndpointSettings,
  EndpointTestResponse,
  EndpointsResponse,
  GatewayTunables,
  KeyUsage,
  ModelCostRow,
  Overview,
  PromptFilterSettings,
  ProxyChain,
  ProxyStatus,
  RequestLogDetail,
  RequestLogOrder,
  RequestLogPage,
  RequestRate,
} from "./types";

export const AUTH_REQUIRED = "Dashboard authentication required";

/** Thrown for any non-2xx dashboard API response. */
export class DashboardApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "DashboardApiError";
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as { detail?: string };
    throw new DashboardApiError(body.detail ?? "Request failed", response.status);
  }
  return response.json() as Promise<T>;
}

export const dashboardApi = {
  login: (password: string) => request<{ ok: boolean }>("/api/dashboard/login", { method: "POST", body: JSON.stringify({ password }) }),
  logout: () => request<{ ok: boolean }>("/api/dashboard/logout", { method: "POST" }),
  overview: () => request<Overview>("/api/dashboard/overview"),
  checkForUpdates: () => request<NonNullable<Overview["version"]>>("/api/dashboard/updates/check", { method: "POST" }),
  installUpdate: (version: string) => request<NonNullable<Overview["update"]>>("/api/dashboard/updates/install", { method: "POST", body: JSON.stringify({ version }) }),
  endpoints: () => request<EndpointsResponse>("/api/dashboard/endpoints"),
  dashboardModels: () => request<DashboardModels>("/api/dashboard/models"),
  saveListedModels: (hidden: string[]) =>
    request<{ hidden: string[] }>("/api/dashboard/models", {
      method: "PUT",
      body: JSON.stringify({ hidden }),
    }),
  refreshModels: () =>
    request<{ refreshed: number; failed: string[]; models: number }>("/api/dashboard/models/refresh", {
      method: "POST",
    }),
  saveEndpoints: (settings: EndpointSettings) =>
    request<{ settings: EndpointSettings }>("/api/dashboard/endpoints", {
      method: "PUT",
      body: JSON.stringify(settings),
    }),
  testEndpoints: (only?: string) =>
    request<EndpointTestResponse>("/api/dashboard/endpoints/test", {
      method: "POST",
      body: JSON.stringify(only ? { only } : {}),
    }),
  pingEndpoints: (reps: number, only?: string) =>
    request<EndpointPingResponse>("/api/dashboard/endpoints/ping", {
      method: "POST",
      body: JSON.stringify(only ? { reps, only } : { reps }),
    }),
  promptFilter: () => request<PromptFilterSettings>("/api/dashboard/prompt-filter"),
  savePromptFilter: (patch: { shortenTools?: boolean; writeHint?: boolean }) =>
    request<PromptFilterSettings>("/api/dashboard/prompt-filter", {
      method: "PUT",
      body: JSON.stringify(patch),
    }),
  accounts: () => request<{ accounts: Account[] }>("/api/dashboard/accounts"),
  deleteAccount: (id: string) =>
    request<{ ok: boolean }>(`/api/dashboard/accounts/${encodeURIComponent(id)}`, { method: "DELETE" }),
  requestLogs: (limit: number, offset: number, model?: string, order?: RequestLogOrder) => {
    const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    if (model) params.set("model", model);
    if (order) params.set("order", order);
    return request<RequestLogPage>(`/api/dashboard/request-logs?${params}`);
  },
  requestLogDetail: (id: number) => request<RequestLogDetail>(`/api/dashboard/request-logs/${id}`),
  dataOverview: () => request<DataOverview>("/api/dashboard/data"),
  clearData: (scope: "logs" | "usage") =>
    request<{ scope: string; affected: number }>("/api/dashboard/data/clear", {
      method: "POST",
      body: JSON.stringify({ scope }),
    }),
  tunables: () => request<GatewayTunables>("/api/dashboard/tunables"),
  saveTunables: (
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
  ) => request<GatewayTunables>("/api/dashboard/tunables", { method: "PUT", body: JSON.stringify(patch) }),
  proxies: () => request<ProxyChain>("/api/dashboard/proxies"),
  saveProxies: (proxies: string[]) =>
    request<{ proxies: ProxyStatus[] }>("/api/dashboard/proxies", {
      method: "PUT",
      body: JSON.stringify({ proxies }),
    }),
  modelCosts: () => request<{ baseline: string; models: ModelCostRow[]; note: string }>("/api/dashboard/model-costs"),
  setAccountEnabled: (label: string, enabled: boolean) =>
    request<{ accountId: string; enabled: boolean; changed: boolean }>(
      `/api/dashboard/accounts/${encodeURIComponent(label)}/enabled`,
      { method: "POST", body: JSON.stringify({ enabled }) },
    ),
  requestRate: (windowSeconds: number, bucketSeconds: number) =>
    request<RequestRate>(`/api/dashboard/request-rate?window=${windowSeconds}&bucket=${bucketSeconds}`),
  apiKeys: () => request<{ apiKeys: ApiKey[] }>("/api/dashboard/keys"),
  keyUsage: () => request<{ usage: KeyUsage }>("/api/dashboard/keys/usage"),
  accountTokenUsage: () => request<{ usage: AccountTokenUsage }>("/api/dashboard/accounts/usage"),
  refreshUsage: () => request<{ accounts: unknown[] }>("/api/dashboard/accounts/refresh-usage", { method: "POST" }),
  createApiKey: (name: string) => request<{ apiKey: string }>("/api/dashboard/keys", { method: "POST", body: JSON.stringify({ name }) }),
  deleteApiKey: (id: string) => request<{ ok: boolean }>(`/api/dashboard/keys/${id}`, { method: "DELETE" }),
  renameApiKey: (id: string, name: string) =>
    request<{ ok: boolean; name: string }>(`/api/dashboard/keys/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ name }),
    }),
  startDeviceLogin: (provider: DeviceLoginProvider) =>
    request<DeviceLoginFlow>("/api/dashboard/accounts/device-login", {
      method: "POST",
      body: JSON.stringify({ provider }),
    }),
  pollDeviceLogin: (flowId: string) => request<DeviceLoginFlow>(`/api/dashboard/accounts/device-login/${flowId}`),
  registerDeviceLogin: (flowId: string) =>
    request<{ accountId: string; initialized: boolean; provider: string; signedOut: string[] }>(
      `/api/dashboard/accounts/device-login/${flowId}/register`,
      { method: "POST" },
    ),
  cancelDeviceLogin: (flowId: string) =>
    request<{ ok: boolean }>(`/api/dashboard/accounts/device-login/${flowId}`, { method: "DELETE" }),
  startBrowserLogin: (provider: "google" | "github") =>
    request<BrowserLoginFlow>("/api/dashboard/accounts/browser-login", {
      method: "POST",
      body: JSON.stringify({ provider }),
    }),
  pollBrowserLogin: (flowId: string) => request<BrowserLoginFlow>(`/api/dashboard/accounts/browser-login/${flowId}`),
  completeBrowserLogin: (flowId: string, url: string) =>
    request<BrowserLoginFlow>(`/api/dashboard/accounts/browser-login/${flowId}/callback`, {
      method: "POST",
      body: JSON.stringify({ url }),
    }),
  registerBrowserLogin: (flowId: string) =>
    request<{ accountId: string; initialized: boolean; provider: string; signedOut: string[] }>(
      `/api/dashboard/accounts/browser-login/${flowId}/register`,
      { method: "POST" },
    ),
  cancelBrowserLogin: (flowId: string) =>
    request<{ ok: boolean }>(`/api/dashboard/accounts/browser-login/${flowId}`, { method: "DELETE" }),
};
