import { useCallback, useEffect, useRef, useState } from "react";
import { AUTH_REQUIRED, dashboardApi } from "./api";
import type { Account, ApiKey, Overview, RequestLogPage, RequestRate } from "./types";

const DEFAULT_PAGE_SIZE = 25;
const EMPTY_LOGS: RequestLogPage = { logs: [], total: 0, limit: DEFAULT_PAGE_SIZE, offset: 0, hasMore: false };
const RATE_WINDOW_SECONDS = 900;
const RATE_BUCKET_SECONDS = 15;
/** Live refresh cadence. One bucket width, so each poll can add at most one column. */
export const REFRESH_INTERVAL_MS = RATE_BUCKET_SECONDS * 1000;

export type DashboardState = {
  overview?: Overview;
  accounts: Account[];
  apiKeys: ApiKey[];
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
  // Pagination reads must not resurrect a stale page after a newer request.
  const logRequestId = useRef(0);

  const handleFailure = useCallback((cause: unknown) => {
    const message = (cause as Error).message;
    setIsAuthenticated(false);
    setOverview(undefined);
    setAccounts([]);
    setApiKeys([]);
    setLogs(EMPTY_LOGS);
    setRate(undefined);
    // A missing session is the expected state before sign-in, not an error.
    setError(message === AUTH_REQUIRED ? "" : message);
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
    try {
      const [nextOverview, nextAccounts, nextKeys, nextRate] = await Promise.all([
        dashboardApi.overview(),
        dashboardApi.accounts(),
        dashboardApi.apiKeys(),
        dashboardApi.requestRate(RATE_WINDOW_SECONDS, RATE_BUCKET_SECONDS),
      ]);
      setOverview(nextOverview);
      setAccounts(nextAccounts.accounts);
      setApiKeys(nextKeys.apiKeys);
      setRate(nextRate);
      setIsAuthenticated(true);
      setLastUpdatedAt(Date.now());
      setError("");
      await loadLogs(limit, offset);
    } catch (cause) {
      handleFailure(cause);
    } finally {
      setIsLoading(false);
    }
  }, [handleFailure, limit, loadLogs, offset]);

  useEffect(() => {
    void reload();
    // Refetch only the log window when pagination changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Live polling. A hidden tab stops fetching and resumes on focus, so a
  // dashboard left open overnight does not keep hitting the API.
  useEffect(() => {
    if (!isLive || !isAuthenticated) return;

    let timer: number | undefined;

    const tick = () => {
      if (document.visibilityState === "visible") void reload();
    };

    const start = () => {
      window.clearInterval(timer);
      timer = window.setInterval(tick, REFRESH_INTERVAL_MS);
    };

    const onVisibility = () => {
      if (document.visibilityState === "visible") {
        void reload();
        start();
      } else {
        window.clearInterval(timer);
      }
    };

    start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [isAuthenticated, isLive, reload]);

  useEffect(() => {
    if (!isAuthenticated) return;
    void loadLogs(limit, offset).catch(handleFailure);
  }, [handleFailure, isAuthenticated, limit, loadLogs, offset]);

  const runAction = useCallback(
    async (action: () => Promise<unknown>) => {
      setIsMutating(true);
      try {
        await action();
        await reload();
      } finally {
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

  return {
    overview,
    accounts,
    apiKeys,
    logs,
    rate,
    isLoading,
    isLogsLoading,
    isAuthenticated,
    isMutating,
    isLive,
    lastUpdatedAt,
    error,
    reload,
    setIsLive,
    runAction,
    signIn,
    signOut,
    setLogLimit,
    setLogOffset: setOffset,
  };
}
