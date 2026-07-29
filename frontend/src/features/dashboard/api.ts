import type { Account, ApiKey, Overview, RegistrationForm, RequestLogPage } from "./types";

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
  accounts: () => request<{ accounts: Account[] }>("/api/dashboard/accounts"),
  requestLogs: (limit: number, offset: number) =>
    request<RequestLogPage>(`/api/dashboard/request-logs?limit=${limit}&offset=${offset}`),
  apiKeys: () => request<{ apiKeys: ApiKey[] }>("/api/dashboard/keys"),
  refreshUsage: () => request<{ accounts: unknown[] }>("/api/dashboard/accounts/refresh-usage", { method: "POST" }),
  createApiKey: (name: string) => request<{ apiKey: string }>("/api/dashboard/keys", { method: "POST", body: JSON.stringify({ name }) }),
  revokeApiKey: (id: string) => request<{ ok: boolean }>(`/api/dashboard/keys/${id}`, { method: "DELETE" }),
  registerAccount: (form: RegistrationForm) =>
    request<{ accountId: string; type: string; initialized: boolean }>("/api/dashboard/accounts", {
      method: "POST",
      body: JSON.stringify(form),
    }),
};
