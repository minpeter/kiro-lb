import { useMemo, useState } from "react";
import { Coins } from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/empty-state";
import { ChartSkeleton } from "./skeletons";
import { exactTokens, formatTokens, shareOf } from "../format";
import type { AccountTokenUsage } from "../types";

/** Rows collapse to a model breakdown on click; more than this and the table dominates the card. */
const VISIBLE_ROWS = 8;

type Row = {
  account: string;
  email: string | null;
  totalTokens: number;
  promptTokens: number;
  completionTokens: number;
  requests: number;
  models: { model: string; totalTokens: number; requests: number }[];
};

function toRows(usage: AccountTokenUsage): Row[] {
  return Object.entries(usage)
    .map(([account, entry]) => ({
      account,
      email: entry.email,
      totalTokens: entry.totalTokens,
      promptTokens: entry.models.reduce((sum, model) => sum + model.promptTokens, 0),
      completionTokens: entry.models.reduce((sum, model) => sum + model.completionTokens, 0),
      requests: entry.requests,
      // Already ordered by tokens from the API, but re-sorted here so the row
      // order does not depend on SQL that a later change might reorder.
      models: [...entry.models]
        .sort((a, b) => b.totalTokens - a.totalTokens)
        .map((model) => ({ model: model.model, totalTokens: model.totalTokens, requests: model.requests })),
    }))
    .sort((a, b) => b.totalTokens - a.totalTokens);
}

function AccountRow({ row, total }: { row: Row; total: number }) {
  const [expanded, setExpanded] = useState(false);
  const share = shareOf(row.totalTokens, total);

  return (
    <li className="border-b border-border/50 last:border-0">
      <button
        type="button"
        onClick={() => setExpanded((open) => !open)}
        aria-expanded={expanded}
        className="flex w-full items-center gap-2 py-1.5 text-left text-sm hover:bg-muted/40"
      >
        <span className="min-w-0 flex-1 truncate">
          {/* Email when the quota poll has supplied one: the hashed label is
              stable but tells an operator nothing about which account this is. */}
          {row.email ?? <span className="font-mono text-xs">{row.account}</span>}
        </span>
        <span className="shrink-0 tabular-nums" title={exactTokens(row.totalTokens)}>
          {formatTokens(row.totalTokens)}
        </span>
        <span className="w-12 shrink-0 text-right text-xs tabular-nums text-muted-foreground">
          {share >= 0.1 ? `${share.toFixed(1)}%` : "<0.1%"}
        </span>
      </button>
      {expanded && (
        <ul className="pb-1.5 pl-4">
          {row.models.map((model) => (
            <li key={model.model} className="flex items-center gap-2 py-0.5 text-xs text-muted-foreground">
              <span className="min-w-0 flex-1 truncate font-mono">{model.model}</span>
              <span className="shrink-0 tabular-nums" title={exactTokens(model.totalTokens)}>
                {formatTokens(model.totalTokens)}
              </span>
              <span className="w-12 shrink-0 text-right tabular-nums">{model.requests.toLocaleString()} req</span>
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

export function AccountTokenPanel({
  accountTokenUsage,
  isLoading,
}: {
  accountTokenUsage: AccountTokenUsage;
  isLoading: boolean;
}) {
  const rows = useMemo(() => toRows(accountTokenUsage), [accountTokenUsage]);
  const total = useMemo(() => rows.reduce((sum, row) => sum + row.totalTokens, 0), [rows]);
  const [showAll, setShowAll] = useState(false);

  const visible = showAll ? rows : rows.slice(0, VISIBLE_ROWS);
  const inputShare = shareOf(
    rows.reduce((sum, row) => sum + row.promptTokens, 0),
    total,
  );

  return (
    <Card className="@container/panel flex flex-col">
      <CardHeader>
        <CardTitle>Token usage by account</CardTitle>
        <CardDescription>
          What each upstream account has served, measured by this gateway. These are local token estimates, not Kiro&apos;s
          quota accounting, which counts requests instead.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex-1">
        {isLoading ? (
          <ChartSkeleton rows={1} />
        ) : total === 0 ? (
          <EmptyState
            icon={Coins}
            title="No tokens recorded yet"
            description="Usage appears once a request completes through /v1. History from before this was recorded cannot be attributed."
          />
        ) : (
          <div className="space-y-4">
            <ul>
              {visible.map((row) => (
                <AccountRow key={row.account} row={row} total={total} />
              ))}
            </ul>
            {rows.length > VISIBLE_ROWS && (
              <button
                type="button"
                onClick={() => setShowAll((open) => !open)}
                className="text-xs text-muted-foreground hover:text-foreground"
              >
                {showAll ? "Show fewer" : `Show all ${rows.length} accounts`}
              </button>
            )}

            <dl className="grid grid-cols-2 gap-3 border-t pt-4 @2xl/panel:grid-cols-4">
              <div>
                <dt className="text-xs text-muted-foreground">Input</dt>
                <dd className="tabular-nums" title={exactTokens(rows.reduce((s, r) => s + r.promptTokens, 0))}>
                  {formatTokens(rows.reduce((s, r) => s + r.promptTokens, 0))}
                  <span className="ml-1 text-xs text-muted-foreground">{inputShare.toFixed(0)}%</span>
                </dd>
              </div>
              <div>
                <dt className="text-xs text-muted-foreground">Output</dt>
                <dd className="tabular-nums" title={exactTokens(rows.reduce((s, r) => s + r.completionTokens, 0))}>
                  {formatTokens(rows.reduce((s, r) => s + r.completionTokens, 0))}
                  <span className="ml-1 text-xs text-muted-foreground">{(100 - inputShare).toFixed(0)}%</span>
                </dd>
              </div>
              <div>
                <dt className="text-xs text-muted-foreground">Requests</dt>
                <dd className="tabular-nums">{rows.reduce((s, r) => s + r.requests, 0).toLocaleString()}</dd>
              </div>
              <div>
                <dt className="text-xs text-muted-foreground">Accounts used</dt>
                <dd className="tabular-nums">{rows.length}</dd>
              </div>
            </dl>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
