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

export type AccountRoutingState =
  | "available"
  | "rate_limited"
  | "quota_exhausted"
  | "cooling_down"
  | "uninitialized";

export type Account = {
  id: string;
  initialized: boolean;
  routingState: AccountRoutingState;
  eligibleInSeconds: number;
  requests: number;
  failures: number;
  cooldownSeconds: number;
  usage?: AccountUsage | null;
};

export type AccountRateSeries = {
  account: string;
  success: number[];
  rateLimited: number[];
  failure: number[];
  peakRpm: number[];
  /** Lowest RPM that drew a 429: the tightest upper bound on the real limit. */
  ceilingRpm: number | null;
  /** Highest RPM served cleanly: a lower bound on the real limit. */
  servedPeakRpm: number;
  rateLimitSamples: number;
};

export type RequestRate = {
  bucketSeconds: number;
  bucketStarts: number[];
  rateWindowSeconds: number;
  accounts: AccountRateSeries[];
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
