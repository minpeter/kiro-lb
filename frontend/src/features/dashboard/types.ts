export type Overview = {
  proxy: { status: string; uptimeSeconds: number };
  requests24h: number;
  successes24h: number;
  averageLatencyMs: number;
  accounts: { total: number; initialized: number };
  models: number;
  reasoning: string;
};

export type AccountUsage = {
  subscriptionTitle?: string;
  subscriptionType?: string;
  currentUsage?: number;
  usageLimit?: number;
  usagePercent?: number;
  unit?: string;
  daysUntilReset?: number;
  overageStatus?: string | null;
  overageUsed?: number | null;
  updatedAt?: number;
  error?: string | null;
};

export type Account = {
  id: string;
  initialized: boolean;
  requests: number;
  failures: number;
  cooldownSeconds: number;
  usage?: AccountUsage | null;
};

export type RequestLog = {
  created_at: number;
  route: string;
  model?: string | null;
  status_code: number;
  latency_ms: number;
};

export type RequestLogPage = {
  logs: RequestLog[];
  total: number;
  limit: number;
  offset: number;
  hasMore: boolean;
};

export type ApiKey = {
  id: string;
  name: string;
  prefix: string;
  createdAt: number;
  revokedAt?: number | null;
};

export type CredentialSource = "sqlite" | "json" | "refresh_token";

export type RegistrationForm = {
  type: CredentialSource;
  path: string;
  refreshToken: string;
  profileArn: string;
  region: string;
  apiRegion: string;
};

export const TAB_IDS = ["overview", "accounts", "keys"] as const;
export type TabId = (typeof TAB_IDS)[number];
