export type Overview = {
  proxy: { status: string; uptimeSeconds: number };
  requests24h: number;
  successes24h: number;
  averageLatencyMs: number;
  accounts: { total: number; initialized: number };
  models: number;
};

export type AccountUsage = {
  /** Upstream account email, present once a quota poll has reported it. */
  email?: string | null;
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
  /** Allowance spent with overage off. Excluded, same as quota_exhausted. */
  | "quota_depleted"
  | "cooling_down"
  | "suspended"
  /**
   * The stored refresh token was rejected by the auth host, so the account
   * cannot obtain a token at all. Outranks every other exclusion and only a
   * re-login clears it.
   */
  | "auth_dead"
  | "uninitialized";

export type Account = {
  id: string;
  initialized: boolean;
  routingState: AccountRoutingState;
  eligibleInSeconds: number;
  /** Unused quota fraction the router weights by, or null when unpolled. */
  quotaHeadroom?: number | null;
  /** Epoch seconds of the next allowance reset, or null when unknown. */
  quotaResetsAt?: number | null;
  quotaOverageEnabled?: boolean | null;
  requests: number;
  failures: number;
  cooldownSeconds: number;
  deletable: boolean;
  enabled?: boolean;
  usage?: AccountUsage | null;
};

export type AccountRateSeries = {
  account: string;
  /**
   * Why the account is or is not a routing target, as of this response. Null
   * when the series outlived the account it came from: rate observations are
   * kept for the window, so a deregistered account still charts.
   */
  routingState: AccountRoutingState | null;
  success: number[];
  rateLimited: number[];
  failure: number[];
  peakRpm: number[];
  /** Lowest RPM that drew a 429 at or above cleanly served traffic, or null. */
  limitRpm: number | null;
  limitUnknownReason: string | null;
  /** Highest RPM proven to succeed. */
  safeRpm: number;
  /** Remaining uncertainty: limitRpm - safeRpm. Smaller is a tighter estimate. */
  limitPrecisionRpm: number | null;
  rateLimitSamples: number;
  informativeSamples: number;
  estimateWindowSeconds: number;
};

export type RequestRate = {
  bucketSeconds: number;
  bucketStarts: number[];
  rateWindowSeconds: number;
  accounts: AccountRateSeries[];
};

export type RequestLog = {
  id?: number;
  created_at: number;
  route: string;
  model?: string | null;
  status_code: number;
  latency_ms: number;
  client_ip?: string | null;
  credits?: number | null;
};

export type RequestLogOrder = "newest" | "oldest";

export type RequestLogDetail = {
  id: number;
  createdAt: number;
  route: string;
  model: string | null;
  statusCode: number;
  latencyMs: number;
  clientIp: string | null;
  userAgent: string | null;
  inputTokens: number | null;
  outputTokens: number | null;
  creditsSpent: number | null;
  modelMultiplier: number | null;
};

export type DataOverview = {
  requestLogs: number;
  oldestLogAt: number | null;
  retentionDays: number;
  databaseBytes: number;
};

export type GatewayTunables = {
  tokenRefreshSeconds: number;
  loadBalancing: string;
  loadBalancingOptions: string[];
  maxConcurrency: number;
  maxAccountConcurrency: number;
  queueTimeoutSeconds: number;
};

export type ProxyStatus = {
  url: string;
  cooling: boolean;
};

export type ProxyChain = {
  proxies: ProxyStatus[];
  schemes: string[];
  cooldownSeconds: number;
};

export type ModelCostRow = {
  model: string;
  multiplier: number;
  contextTokens: number | null;
};

export type RequestLogPage = {
  logs: RequestLog[];
  total: number;
  limit: number;
  offset: number;
  hasMore: boolean;
  models?: string[];
  order?: RequestLogOrder;
};

export type ApiKey = {
  id: string;
  name: string;
  prefix: string;
  createdAt: number | null;
  revokedAt?: number | null;
  /** The environment root key: attributable in usage, but not editable here. */
  readOnly: boolean;
};

export type KeyModelUsage = {
  model: string;
  promptTokens: number;
  completionTokens: number;
  totalTokens: number;
  requests: number;
  generationSeconds: number;
  tokensPerSecond: number | null;
  updatedAt: number;
};

export type KeyUsage = Record<string, KeyModelUsage[]>;

/**
 * What one account spent, by model. Same row shape as the per-key breakdown, so
 * the token formatting and slice helpers are shared rather than reimplemented.
 *
 * Distinct from `AccountUsage` above, which is the upstream *quota* reading for
 * an account. This is what this gateway measured; that is what Kiro reports.
 */
export type AccountTokenUsageEntry = {
  /** Absent until the usage poll has read the account's profile. */
  email: string | null;
  models: KeyModelUsage[];
  totalTokens: number;
  requests: number;
};

/** Keyed by the hashed account label, matching the accounts panel. */
export type AccountTokenUsage = Record<string, AccountTokenUsageEntry>;

export type DeviceLoginProvider = "builder-id" | "google" | "github";

export type DeviceLoginFlow = {
  flowId: string;
  provider: string;
  status: "pending" | "approved" | "failed" | "expired";
  detail: string | null;
  userCode: string;
  verificationUri: string;
  verificationUriComplete: string;
  expiresInSeconds: number;
};

export const TAB_IDS = ["overview", "accounts", "keys", "settings", "info"] as const;
export type TabId = (typeof TAB_IDS)[number];

export interface EndpointOption {
  key: string;
  name: string;
  url: string;
}

export interface EndpointSettings {
  rotation: boolean;
  order: string[];
  cooldownSeconds: number;
}

export interface EndpointsResponse {
  available: EndpointOption[];
  settings: EndpointSettings;
  pingRepsMax: number;
  pingRepsDefault: number;
}

export interface EndpointTestResult {
  key: string;
  name: string;
  ok: boolean;
  statusCode: number | null;
  ttfbMs: number | null;
  error: string | null;
}

export interface EndpointTestResponse {
  model: string;
  requestsSpent: number;
  results: EndpointTestResult[];
}

export interface EndpointPingResult {
  key: string;
  name: string;
  samples: number;
  medianMs: number | null;
  minMs: number | null;
  maxMs: number | null;
  failures: string[];
}

export interface EndpointPingResponse {
  model: string;
  reps: number;
  requestsSpent: number;
  results: EndpointPingResult[];
  fastest: string | null;
  conclusive: boolean;
  betweenSpreadMs?: number;
  withinSpreadMs?: number;
  verdict: string;
}

export interface PromptFilterSettings {
  enabled: boolean;
  identity: string;
  preservedNote: string;
  droppedSections: string[];
}

export interface AgentModeSettings {
  mode: string;
  allowed: string[];
}
