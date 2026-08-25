import { useCallback, useEffect, useRef, useState } from "react";
import { AUTH_REQUIRED, DashboardApiError, dashboardApi } from "./api";
import type {
  Account,
  AccountTokenUsage,
  ApiKey,
  KeyUsage,
  Overview,
  RequestLogPage,
  RequestRate,
} from "./types";

const DEFAULT_PAGE_SIZE = 25;
const EMPTY_LOGS: RequestLogPage = { logs: [], total: 0, limit: DEFAULT_PAGE_SIZE, offset: 0, hasMore: false };
const RATE_WINDOW_SECONDS = 900;
const RATE_BUCKET_SECONDS = 5;
/** Live refresh cadence. Each poll costs ~2ms of server work, all of it local. */
export const REFRESH_INTERVAL_MS = 1000;
/** Ceiling for the failure backoff so a long outage still recovers within seconds. */
export const MAX_REFRESH_INTERVAL_MS = 10_000;

export type DashboardState = {
  overview?: Overview;
  accounts: Account[];
  apiKeys: ApiKey[];
  keyUsage: KeyUsage;
  accountTokenUsage: AccountTokenUsage;
  logs: RequestLogPage;
  rate?: RequestRate;
  /** True only for the initial load, so refreshes do not flash skeletons. */
  isLoading: boolean;
  isLogsLoading: boolean;
  isAuthenticated: boolean;
  isMutating: boolean;
  isLive: boolean;
  lastUpdatedAt?: number;
  error: string;
  /** Non-auth fetch failure while data is kept on screen; null when healthy. */
  connectionError: string | null;
  /** Last failed mutation (revoke/delete/refresh); dismissed via clearActionError. */
  actionError: string | null;
  clearActionError: () => void;
  reload: () => Promise<void>;
  setIsLive: (live: boolean) => void;
  runAction: (action: () => Promise<unknown>) => Promise<void>;
  signIn: (password: string) => Promise<void>;
  signOut: () => Promise<void>;
  setLogLimit: (limit: number) => void;
  setLogOffset: (offset: number) => void;
};

export function useDashboard(): DashboardState {
  const [overview, setOverview] = useState<Overview>();
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [apiKeys, setApiKeys] = useState<ApiKey[]>([]);
  const [keyUsage, setKeyUsage] = useState<KeyUsage>({});
  const [accountTokenUsage, setAccountTokenUsage] = useState<AccountTokenUsage>({});
  const [logs, setLogs] = useState<RequestLogPage>(EMPTY_LOGS);
  const [rate, setRate] = useState<RequestRate>();
  const [isLive, setIsLive] = useState(true);
  const [lastUpdatedAt, setLastUpdatedAt] = useState<number>();
  const [limit, setLimit] = useState(DEFAULT_PAGE_SIZE);
  const [offset, setOffset] = useState(0);
  const [isLoading, setIsLoading] = useState(true);
  const [isLogsLoading, setIsLogsLoading] = useState(false);
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [isMutating, setIsMutating] = useState(false);
  const [error, setError] = useState("");
  // Appended after the states above on purpose: use-dashboard.test.ts addresses
  // useState calls by index, so new state must never shift existing indices.
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  // Pagination reads must not resurrect a stale page after a newer request.
  const logRequestId = useRef(0);
  // Reloads and live polls share this generation so stale responses cannot overwrite current dashboard state.
  const dashboardRequestId = useRef(0);
  // Live-poll cadence with failure backoff; a ref so a failed tick can slow the
  // next arm without re-running the polling effect.
  const pollDelayRef = useRef(REFRESH_INTERVAL_MS);

  const handleFailure = useCallback((cause: unknown) => {
    const message = (cause as Error).message;
    const isAuthFailure = (cause instanceof DashboardApiError && cause.status === 401) || message === AUTH_REQUIRED;
    if (!isAuthFailure) {
      // A blip, restart, or upstream 5xx: keep the last-known data on screen
      // and slow the poll instead of dumping the operator at the login card.
      setConnectionError(message || "Request failed");
      pollDelayRef.current = Math.min(pollDelayRef.current * 2, MAX_REFRESH_INTERVAL_MS);
      return;
    }
    setIsAuthenticated(false);
    setOverview(undefined);
    setAccounts([]);
    setApiKeys([]);
    setLogs(EMPTY_LOGS);
    setRate(undefined);
    setKeyUsage({});
    setAccountTokenUsage({});
    setConnectionError(null);
    // A missing session is the expected state before sign-in, not an error.
    setError(message === AUTH_REQUIRED ? "" : message);
  }, []);

  const noteSuccess = useCallback(() => {
    setIsAuthenticated(true);
    setLastUpdatedAt(Date.now());
    setConnectionError(null);
    pollDelayRef.current = REFRESH_INTERVAL_MS;
  }, []);

  const loadLogs = useCallback(async (nextLimit: number, nextOffset: number) => {
    const requestId = ++logRequestId.current;
    setIsLogsLoading(true);
    try {
      const page = await dashboardApi.requestLogs(nextLimit, nextOffset);
      if (requestId !== logRequestId.current) return;
      // Deleted or expired rows can leave the offset past the end of the table.
      if (page.logs.length === 0 && page.total > 0 && nextOffset > 0) {
        setOffset(0);
        return;
      }
      setLogs(page);
    } finally {
      if (requestId === logRequestId.current) setIsLogsLoading(false);
    }
  }, []);

  const reload = useCallback(async () => {
    const requestId = ++dashboardRequestId.current;
    try {
      const [nextOverview, nextAccounts, nextKeys, nextRate, nextKeyUsage, nextAccountUsage] = await Promise.all([
        dashboardApi.overview(),
        dashboardApi.accounts(),
        dashboardApi.apiKeys(),
        dashboardApi.requestRate(RATE_WINDOW_SECONDS, RATE_BUCKET_SECONDS),
        dashboardApi.keyUsage(),
        dashboardApi.accountTokenUsage(),
      ]);
      if (requestId !== dashboardRequestId.current) return;
      setOverview(nextOverview);
      setAccounts(nextAccounts.accounts);
      setApiKeys(nextKeys.apiKeys);
      setRate(nextRate);
      setKeyUsage(nextKeyUsage.usage);
      setAccountTokenUsage(nextAccountUsage.usage);
      noteSuccess();
      setError("");
      await loadLogs(limit, offset);
    } catch (cause) {
      if (requestId === dashboardRequestId.current) handleFailure(cause);
    } finally {
      if (requestId === dashboardRequestId.current) setIsLoading(false);
    }
  }, [handleFailure, limit, loadLogs, noteSuccess, offset]);

  // Live tick. Only the time-varying panels are refetched: API keys do not
  // change on their own, and refetching the log page every second would fight
  // the operator's pagination.
  const refreshLive = useCallback(async () => {
    const requestId = ++dashboardRequestId.current;
    try {
      const [nextOverview, nextAccounts, nextRate, nextKeyUsage, nextAccountUsage] = await Promise.all([
        dashboardApi.overview(),
        dashboardApi.accounts(),
        dashboardApi.requestRate(RATE_WINDOW_SECONDS, RATE_BUCKET_SECONDS),
        dashboardApi.keyUsage(),
        dashboardApi.accountTokenUsage(),
      ]);
      if (requestId !== dashboardRequestId.current) return;
      setOverview(nextOverview);
      setAccounts(nextAccounts.accounts);
      setRate(nextRate);
      setKeyUsage(nextKeyUsage.usage);
      setAccountTokenUsage(nextAccountUsage.usage);
      noteSuccess();
    } catch (cause) {
      if (requestId === dashboardRequestId.current) handleFailure(cause);
    }
  }, [handleFailure, noteSuccess]);

  useEffect(() => {
    void reload();
    // Refetch only the log window when pagination changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Live polling. A hidden tab stops fetching and resumes on focus, so a
  // dashboard left open overnight does not keep hitting the API. Overlapping
  // ticks are impossible because the next timer is armed after the fetch settles.
  useEffect(() => {
    if (!isLive || !isAuthenticated) return;

    let stopped = false;
    let timer: number | undefined;

    const tick = async () => {
      if (document.visibilityState === "visible") await refreshLive();
      if (!stopped) timer = window.setTimeout(tick, pollDelayRef.current);
    };

    timer = window.setTimeout(tick, pollDelayRef.current);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [isAuthenticated, isLive, refreshLive]);

  useEffect(() => {
    if (!isAuthenticated) return;
    void loadLogs(limit, offset).catch(handleFailure);
  }, [handleFailure, isAuthenticated, limit, loadLogs, offset]);

  const runAction = useCallback(
    async (action: () => Promise<unknown>) => {
      setIsMutating(true);
      try {
        await action();
        setActionError(null);
      } catch (cause) {
        // A failed mutation must be visible; swallowing it here is what made
        // Revoke/Delete look successful while doing nothing.
        setActionError((cause as Error).message || "Action failed");
      } finally {
        // Resync regardless: a partial mutation is worse when the screen keeps
        // showing the pre-action state.
        await reload();
        setIsMutating(false);
      }
    },
    [reload],
  );

  const signIn = useCallback(
    async (password: string) => {
      try {
        await dashboardApi.login(password);
        setError("");
        await reload();
      } catch (cause) {
        setError((cause as Error).message);
      }
    },
    [reload],
  );

  const signOut = useCallback(async () => {
    await dashboardApi.logout();
    await reload();
  }, [reload]);

  const setLogLimit = useCallback((next: number) => {
    setLimit(next);
    setOffset(0);
  }, []);

  const clearActionError = useCallback(() => setActionError(null), []);

  return {
    overview,
    accounts,
    apiKeys,
    keyUsage,
    accountTokenUsage,
    logs,
    rate,
    isLoading,
    isLogsLoading,
    isAuthenticated,
    isMutating,
    isLive,
    lastUpdatedAt,
    error,
    connectionError,
    actionError,
    clearActionError,
    reload,
    setIsLive,
    runAction,
    signIn,
    signOut,
    setLogLimit,
    setLogOffset: setOffset,
  };
}
