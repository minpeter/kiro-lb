import { useCallback, useEffect, useRef, useState } from "react";
import { AUTH_REQUIRED, dashboardApi } from "./api";
import type { Account, ApiKey, Overview, RequestLogPage } from "./types";

const DEFAULT_PAGE_SIZE = 25;
const EMPTY_LOGS: RequestLogPage = { logs: [], total: 0, limit: DEFAULT_PAGE_SIZE, offset: 0, hasMore: false };

export type DashboardState = {
  overview?: Overview;
  accounts: Account[];
  apiKeys: ApiKey[];
  logs: RequestLogPage;
  /** True only for the initial load, so refreshes do not flash skeletons. */
  isLoading: boolean;
  isLogsLoading: boolean;
  isAuthenticated: boolean;
  isMutating: boolean;
  error: string;
  reload: () => Promise<void>;
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
      const [nextOverview, nextAccounts, nextKeys] = await Promise.all([
        dashboardApi.overview(),
        dashboardApi.accounts(),
        dashboardApi.apiKeys(),
      ]);
      setOverview(nextOverview);
      setAccounts(nextAccounts.accounts);
      setApiKeys(nextKeys.apiKeys);
      setIsAuthenticated(true);
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
    isLoading,
    isLogsLoading,
    isAuthenticated,
    isMutating,
    error,
    reload,
    runAction,
    signIn,
    signOut,
    setLogLimit,
    setLogOffset: setOffset,
  };
}
