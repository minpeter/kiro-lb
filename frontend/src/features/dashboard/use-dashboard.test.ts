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
  timers: Timer[];
  api: {
    accounts: ReturnType<typeof vi.fn>;
    apiKeys: ReturnType<typeof vi.fn>;
    keyUsage: ReturnType<typeof vi.fn>;
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
  timers: [],
  api: {
    accounts: vi.fn(),
    apiKeys: vi.fn(),
    keyUsage: vi.fn(),
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

vi.mock("react", () => {
  const useEffect = (effect: Effect) => {
    harness.effects.push(effect);
  };
  const useRef = <T,>(initial: T): { current: T } => ({ current: initial });
  function useState<T>(initial: T): [T, (value: T) => void];
  function useState(initial: unknown): [unknown, (value: unknown) => void] {
    const index = harness.stateIndex++;
    return [index === 12 ? true : initial, (value: unknown) => {
      if (index === 0) harness.latestOverview = value as Overview;
      if (index === 1 && isAccountArray(value)) harness.latestAccounts = value;
      if (index === 5) harness.latestRate = value as RequestRate;
    }];
  }
  return { useCallback: <T,>(callback: T) => callback, useEffect, useRef, useState };
});

vi.mock("./api", () => ({ AUTH_REQUIRED: "Authentication required", dashboardApi: harness.api }));

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
    harness.timers = [];
    Object.values(harness.api).forEach((method) => method.mockReset());
    harness.api.requestLogs.mockResolvedValue(logs);
    vi.stubGlobal("document", { visibilityState: "visible" });
    vi.stubGlobal("window", {
      clearTimeout: vi.fn(),
      setTimeout: (callback: Timer) => {
        harness.timers.push(callback);
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
});
