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
  usage?: AccountUsage | null;
};

export type AccountRateSeries = {
  account: string;
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

export const TAB_IDS = ["overview", "accounts", "keys"] as const;
export type TabId = (typeof TAB_IDS)[number];
