import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Account, Overview, RequestLogPage, RequestRate } from "./types";

const realSetTimeout = globalThis.setTimeout;
const realClearTimeout = globalThis.clearTimeout;

type Effect = () => void | (() => void);
type Timer = () => Promise<void>;

interface Harness {
  effects: Effect[];
  stateIndex: number;
  latestAccounts: Account[];
  latestOverview?: Overview;
  latestRate?: RequestRate;
  latestAuth?: boolean;
  latestConnectionError?: string | null;
  latestActionError?: string | null;
  timers: Timer[];
  timerDelays: number[];
  api: {
    accounts: ReturnType<typeof vi.fn>;
    apiKeys: ReturnType<typeof vi.fn>;
    keyUsage: ReturnType<typeof vi.fn>;
    accountTokenUsage: ReturnType<typeof vi.fn>;
    deleteAccount: ReturnType<typeof vi.fn>;
    login: ReturnType<typeof vi.fn>;
    logout: ReturnType<typeof vi.fn>;
    overview: ReturnType<typeof vi.fn>;
    requestLogs: ReturnType<typeof vi.fn>;
    requestRate: ReturnType<typeof vi.fn>;
  };
}

const harness = vi.hoisted((): Harness => ({
  effects: [],
  stateIndex: 0,
  latestAccounts: [],
  latestOverview: undefined,
  latestRate: undefined,
  latestAuth: undefined,
  latestConnectionError: undefined,
  latestActionError: undefined,
  timers: [],
  timerDelays: [],
  api: {
    accounts: vi.fn(),
    apiKeys: vi.fn(),
    keyUsage: vi.fn(),
    accountTokenUsage: vi.fn(),
    deleteAccount: vi.fn(),
    login: vi.fn(),
    logout: vi.fn(),
    overview: vi.fn(),
    requestLogs: vi.fn(),
    requestRate: vi.fn(),
  },
}));

const isAccountArray = (value: unknown): value is Account[] =>
  Array.isArray(value) && value.every((account) => typeof account === "object" && account !== null && "id" in account);

// The hook's useState calls are identified by call order, so these indices must
// track use-dashboard.ts. Named rather than inlined as magic numbers: adding a
// state above one of them shifts every index below, which is exactly how the
// accountTokenUsage state broke this file.
const STATE_OVERVIEW = 0;
const STATE_ACCOUNTS = 1;
const STATE_RATE = 6;
// Forced true so the live-polling effect registers its interval; the hook only
// polls once authenticated. Previously the literal 12, which the new
// accountTokenUsage state shifted to 13.
const STATE_IS_AUTHENTICATED = 13;
// New states are appended at the end of the hook precisely so the indices
// above stay put.
const STATE_CONNECTION_ERROR = 16;
const STATE_ACTION_ERROR = 17;

vi.mock("react", () => {
  const useEffect = (effect: Effect) => {
    harness.effects.push(effect);
  };
  const useRef = <T,>(initial: T): { current: T } => ({ current: initial });
  function useState<T>(initial: T): [T, (value: T) => void];
  function useState(initial: unknown): [unknown, (value: unknown) => void] {
    const index = harness.stateIndex++;
    return [index === STATE_IS_AUTHENTICATED ? true : initial, (value: unknown) => {
      if (index === STATE_OVERVIEW) harness.latestOverview = value as Overview;
      if (index === STATE_ACCOUNTS && isAccountArray(value)) harness.latestAccounts = value;
      if (index === STATE_RATE) harness.latestRate = value as RequestRate;
      if (index === STATE_IS_AUTHENTICATED) harness.latestAuth = value as boolean;
      if (index === STATE_CONNECTION_ERROR) harness.latestConnectionError = value as string | null;
      if (index === STATE_ACTION_ERROR) harness.latestActionError = value as string | null;
    }];
  }
  return { useCallback: <T,>(callback: T) => callback, useEffect, useRef, useState };
});

const mocked = vi.hoisted(() => {
  class DashboardApiError extends Error {
    readonly status: number;
    constructor(message: string, status: number) {
      super(message);
      this.name = "DashboardApiError";
      this.status = status;
    }
  }
  return { DashboardApiError };
});
const { DashboardApiError } = mocked;

vi.mock("./api", () => ({
  AUTH_REQUIRED: "Authentication required",
  DashboardApiError: mocked.DashboardApiError,
  dashboardApi: harness.api,
}));

import { useDashboard } from "./use-dashboard";

const accounts = (ids: string[]): { accounts: Account[] } => ({
  accounts: ids.map((id) => ({
    id,
    initialized: true,
    routingState: "available",
    eligibleInSeconds: 0,
    requests: 0,
    failures: 0,
    cooldownSeconds: 0,
    deletable: true,
  })),
});

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
};

const awaitCompletion = <T,>(completion: Promise<T>, timeoutMs: number): Promise<T> =>
  new Promise((resolve, reject) => {
    const timeout = realSetTimeout(
      () => reject(new Error(`Timed out waiting for refresh completion after ${timeoutMs}ms`)),
      timeoutMs,
    );
    void completion.then(
      (value) => {
        realClearTimeout(timeout);
        resolve(value);
      },
      (cause: unknown) => {
        realClearTimeout(timeout);
        reject(cause);
      },
    );
  });

const logs: RequestLogPage = { logs: [], total: 0, limit: 25, offset: 0, hasMore: false };

describe("useDashboard", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    harness.effects = [];
    harness.stateIndex = 0;
    harness.latestAccounts = [];
    harness.latestOverview = undefined;
    harness.latestRate = undefined;
    harness.latestAuth = undefined;
    harness.latestConnectionError = undefined;
    harness.latestActionError = undefined;
    harness.timers = [];
    harness.timerDelays = [];
    Object.values(harness.api).forEach((method) => method.mockReset());
    harness.api.requestLogs.mockResolvedValue(logs);
    vi.stubGlobal("document", { visibilityState: "visible" });
    vi.stubGlobal("window", {
      clearTimeout: vi.fn(),
      setTimeout: (callback: Timer, delay?: number) => {
        harness.timers.push(callback);
        harness.timerDelays.push(delay ?? 0);
        return harness.timers.length;
      },
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("keeps the latest accounts, overview, and rate when an older live refresh settles last", async () => {
    const staleAccounts = deferred<{ accounts: Account[] }>();
    const freshAccounts = deferred<{ accounts: Account[] }>();
    const staleOverview = deferred<Overview>();
    const freshOverview = deferred<Overview>();
    const staleRate = deferred<RequestRate>();
    const freshRate = deferred<RequestRate>();
    const staleUsage = deferred<{ usage: object }>();
    const freshUsage = deferred<{ usage: object }>();

    harness.api.accounts.mockReturnValueOnce(staleAccounts.promise).mockReturnValueOnce(freshAccounts.promise);
    harness.api.overview.mockReturnValueOnce(staleOverview.promise).mockReturnValueOnce(freshOverview.promise);
    harness.api.requestRate.mockReturnValueOnce(staleRate.promise).mockReturnValueOnce(freshRate.promise);
    harness.api.keyUsage.mockReturnValueOnce(staleUsage.promise).mockReturnValueOnce(freshUsage.promise);
    // Resolved rather than deferred: this test is about the accounts/overview/rate
    // race, and leaving the new call pending would stall the Promise.all it sits in.
    harness.api.accountTokenUsage.mockResolvedValue({ usage: {} });
    harness.api.apiKeys.mockResolvedValue({ apiKeys: [] });
    harness.api.deleteAccount.mockResolvedValue({ ok: true });

    const dashboard = useDashboard();
    harness.effects[1]!();
    const staleRefresh = harness.timers[0]!();
    expect(harness.api.accounts).toHaveBeenCalledTimes(1);

    const reload = dashboard.runAction(() => harness.api.deleteAccount("removed"));
    freshAccounts.resolve(accounts(["survivor"]));
    freshOverview.resolve({ proxy: { status: "healthy", uptimeSeconds: 2 }, requests24h: 2, successes24h: 2, averageLatencyMs: 2, accounts: { total: 1, initialized: 1 }, models: 2 });
    freshRate.resolve({ bucketSeconds: 5, bucketStarts: [2], rateWindowSeconds: 900, accounts: [] });
    freshUsage.resolve({ usage: {} });
    await reload;

    staleAccounts.resolve(accounts(["removed", "survivor"]));
    staleOverview.resolve({ proxy: { status: "healthy", uptimeSeconds: 1 }, requests24h: 1, successes24h: 1, averageLatencyMs: 1, accounts: { total: 2, initialized: 2 }, models: 1 });
    staleRate.resolve({ bucketSeconds: 5, bucketStarts: [1], rateWindowSeconds: 900, accounts: [] });
    staleUsage.resolve({ usage: {} });
    await awaitCompletion(staleRefresh, 1_000);

    expect({
      accountIds: harness.latestAccounts.map((account) => account.id),
      overviewRequests: harness.latestOverview?.requests24h,
      rateBuckets: harness.latestRate?.bucketStarts,
    }).toEqual({ accountIds: ["survivor"], overviewRequests: 2, rateBuckets: [2] });
  });

  const healthyOverview = (): Overview => ({
    proxy: { status: "healthy", uptimeSeconds: 1 },
    requests24h: 1,
    successes24h: 1,
    averageLatencyMs: 1,
    accounts: { total: 1, initialized: 1 },
    models: 1,
  });

  const mockHealthyLoad = () => {
    harness.api.overview.mockResolvedValue(healthyOverview());
    harness.api.accounts.mockResolvedValue(accounts(["a"]));
    harness.api.apiKeys.mockResolvedValue({ apiKeys: [] });
    harness.api.requestRate.mockResolvedValue({ bucketSeconds: 5, bucketStarts: [1], rateWindowSeconds: 900, accounts: [] });
    harness.api.keyUsage.mockResolvedValue({ usage: {} });
    harness.api.accountTokenUsage.mockResolvedValue({ usage: {} });
  };

  it("keeps data and reports a connection error on a non-401 failure, then clears it on recovery", async () => {
    mockHealthyLoad();
    const dashboard = useDashboard();
    await awaitCompletion(dashboard.reload(), 1_000);
    expect(harness.latestOverview?.requests24h).toBe(1);

    harness.api.overview.mockRejectedValue(new DashboardApiError("upstream exploded", 500));
    await awaitCompletion(dashboard.reload(), 1_000);

    expect({
      overviewKept: harness.latestOverview?.requests24h,
      accountsKept: harness.latestAccounts.map((account) => account.id),
      deAuthed: harness.latestAuth,
      connectionError: harness.latestConnectionError,
    }).toEqual({ overviewKept: 1, accountsKept: ["a"], deAuthed: true, connectionError: "upstream exploded" });

    harness.api.overview.mockResolvedValue(healthyOverview());
    await awaitCompletion(dashboard.reload(), 1_000);
    expect(harness.latestConnectionError).toBeNull();
  });

  it("de-authenticates and clears data only on a 401 failure", async () => {
    mockHealthyLoad();
    const dashboard = useDashboard();
    await awaitCompletion(dashboard.reload(), 1_000);

    harness.api.overview.mockRejectedValue(new DashboardApiError("Dashboard authentication required", 401));
    await awaitCompletion(dashboard.reload(), 1_000);

    expect({ deAuthed: harness.latestAuth, overviewCleared: harness.latestOverview }).toEqual({
      deAuthed: false,
      overviewCleared: undefined,
    });
  });

  it("surfaces a failed action instead of throwing, and still resyncs", async () => {
    mockHealthyLoad();
    harness.api.deleteAccount.mockRejectedValue(new DashboardApiError("delete rejected", 500));
    const dashboard = useDashboard();
    await awaitCompletion(dashboard.reload(), 1_000);
    const overviewCallsBefore = harness.api.overview.mock.calls.length;

    await awaitCompletion(dashboard.runAction(() => harness.api.deleteAccount("a")), 1_000);

    expect({
      actionError: harness.latestActionError,
      resynced: harness.api.overview.mock.calls.length > overviewCallsBefore,
    }).toEqual({ actionError: "delete rejected", resynced: true });
  });

  it("backs off the live poll while failing and resets the cadence on recovery", async () => {
    mockHealthyLoad();
    const dashboard = useDashboard();
    await awaitCompletion(dashboard.reload(), 1_000);

    harness.effects[1]!();
    expect(harness.timerDelays.at(-1)).toBe(1_000);

    harness.api.overview.mockRejectedValue(new DashboardApiError("blip", 502));
    await awaitCompletion(harness.timers.at(-1)!(), 1_000);
    expect(harness.timerDelays.at(-1)).toBe(2_000);

    await awaitCompletion(harness.timers.at(-1)!(), 1_000);
    expect(harness.timerDelays.at(-1)).toBe(4_000);

    harness.api.overview.mockResolvedValue(healthyOverview());
    await awaitCompletion(harness.timers.at(-1)!(), 1_000);
    expect(harness.timerDelays.at(-1)).toBe(1_000);
  });
});
