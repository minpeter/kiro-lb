import { useCallback, useEffect, useState } from "react";
import { TAB_IDS, type TabId } from "./types";

const isTabId = (value: string): value is TabId => (TAB_IDS as readonly string[]).includes(value);
const readHash = (): TabId => {
  const candidate = window.location.hash.replace(/^#/, "");
  return isTabId(candidate) ? candidate : "overview";
};

/** Keeps the active tab in the URL hash so reloads and history navigation work. */
export function useTabHash(): [TabId, (value: string) => void] {
  const [tab, setTab] = useState<TabId>(readHash);

  useEffect(() => {
    const sync = () => setTab(readHash());
    window.addEventListener("hashchange", sync);
    return () => window.removeEventListener("hashchange", sync);
  }, []);

  const selectTab = useCallback((value: string) => {
    const next = isTabId(value) ? value : "overview";
    setTab(next);
    if (readHash() !== next) window.location.hash = next;
  }, []);

  return [tab, selectTab];
}
