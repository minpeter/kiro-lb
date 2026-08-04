import { useState } from "react";
import { AtSign, Ban, ServerCog, Trash2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/empty-state";
import { Progress } from "@/components/ui/progress";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatDuration, formatTimestamp, formatUsage } from "../format";
import type { Account, AccountRoutingState } from "../types";
import { TableSkeleton } from "./skeletons";

const ROUTING_STATE_LABEL: Record<AccountRoutingState, string> = {
  available: "ready",
  rate_limited: "rate limited",
  quota_exhausted: "quota exhausted",
  cooling_down: "cooling down",
  suspended: "BANNED",
  uninitialized: "pending",
};

/** Only "ready" is a routing target; everything else is currently excluded. */
function RoutingStateCell({ account }: { account: Account }) {
  const state = account.routingState;
  const label = ROUTING_STATE_LABEL[state] ?? state;
  const variant = state === "available" ? "secondary" : state === "uninitialized" ? "outline" : "destructive";
  const suspended = state === "suspended";
  return (
    <div className="space-y-1">
      <Badge variant={variant} className={suspended ? "font-semibold tracking-wide" : undefined}>
        {suspended && <Ban size={12} />}
        {label}
      </Badge>
      {suspended ? (
        <p className="text-xs text-destructive">locked by Kiro; contact support</p>
      ) : (
        account.eligibleInSeconds > 0 && (
          <p className="text-xs tabular-nums text-muted-foreground">
            back in {formatDuration(account.eligibleInSeconds)}
          </p>
        )
      )}
    </div>
  );
}

/**
 * Identity of one pool account: the upstream email when the quota poll has
 * reported it, with the hashed credential label underneath. The label stays
 * visible either way because it is what client-facing 503 diagnostics name.
 */
function AccountCell({ account }: { account: Account }) {
  const email = account.usage?.email;
  if (!email) return <span className="font-mono text-xs">{account.id}</span>;
  return (
    <div className="flex min-w-0 flex-col gap-0.5">
      <span className="flex min-w-0 items-center gap-1.5 font-medium" title={email}>
        <AtSign size={13} className="shrink-0 text-muted-foreground" />
        <span className="truncate">{email}</span>
      </span>
      <span className="pl-[1.15rem] font-mono text-xs text-muted-foreground">{account.id}</span>
    </div>
  );
}

function UsageCell({ account }: { account: Account }) {
  const usage = account.usage;
  if (usage?.error) return <span className="text-xs text-destructive">{usage.error}</span>;
  if (!usage || usage.usagePercent == null) return <span className="text-muted-foreground">—</span>;
  return (
    <div className="min-w-40 space-y-1.5">
      <Progress value={Math.min(usage.usagePercent, 100)} className="h-1.5" />
      <p className="text-xs tabular-nums text-muted-foreground">{formatUsage(usage)}</p>
    </div>
  );
}

export type AccountsPanelProps = {
  accounts: Account[];
  isLoading: boolean;
  isMutating?: boolean;
  onDeleteAccount?: (id: string) => void;
};

export function AccountsPanel({ accounts, isLoading, isMutating, onDeleteAccount }: AccountsPanelProps) {
  const [confirmingId, setConfirmingId] = useState<string | null>(null);
  return (
    <Card>
      <CardHeader>
        <CardTitle>Accounts &amp; live quota</CardTitle>
        <CardDescription>
          Fetched from Kiro getUsageLimits. Tokens, profile ARNs, and raw upstream bodies are never stored.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <TableSkeleton rows={2} columns={10} />
        ) : accounts.length === 0 ? (
          <EmptyState icon={ServerCog} title="No accounts registered" description="Add a credential source below." />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Account</TableHead>
                <TableHead>State</TableHead>
                <TableHead>Plan</TableHead>
                <TableHead>Overage</TableHead>
                <TableHead>Usage</TableHead>
                <TableHead className="text-right">Reset</TableHead>
                <TableHead className="text-right">Requests</TableHead>
                <TableHead className="text-right">Failures</TableHead>
                <TableHead>Updated</TableHead>
                <TableHead className="w-12" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {accounts.map((account) => (
                <TableRow key={account.id}>
                  <TableCell className="max-w-56">
                    <AccountCell account={account} />
                  </TableCell>
                  <TableCell>
                    <RoutingStateCell account={account} />
                  </TableCell>
                  <TableCell>{account.usage?.subscriptionTitle ?? "—"}</TableCell>
                  <TableCell>
                    {account.usage?.overageStatus == null || account.usage.overageStatus === "UNKNOWN" ? (
                      "—"
                    ) : (
                      <Badge variant={account.usage.overageStatus === "DISABLED" ? "outline" : "secondary"}>
                        {account.usage.overageStatus.toLowerCase()}
                        {account.usage.overageUsed ? ` · ${account.usage.overageUsed.toFixed(2)}` : ""}
                      </Badge>
                    )}
                  </TableCell>
                  <TableCell>
                    <UsageCell account={account} />
                  </TableCell>
                  <TableCell className="text-right tabular-nums">
                    {account.usage?.daysUntilReset == null ? "—" : `${account.usage.daysUntilReset}d`}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{account.requests}</TableCell>
                  <TableCell className="text-right tabular-nums">{account.failures}</TableCell>
                  <TableCell className="text-xs text-muted-foreground">{formatTimestamp(account.usage?.updatedAt)}</TableCell>
                  <TableCell className="text-right">
                    {account.deletable ? (
                      confirmingId === account.id ? (
                        <div className="flex items-center justify-end gap-1">
                          <Button
                            size="xs"
                            variant="outline"
                            disabled={isMutating}
                            aria-label={`Cancel deleting account ${account.id}`}
                            onClick={() => setConfirmingId(null)}
                          >
                            Cancel
                          </Button>
                          <Button
                            size="xs"
                            variant="destructive"
                            disabled={isMutating}
                            aria-label={`Confirm delete account ${account.id}`}
                            onClick={() => {
                              setConfirmingId(null);
                              onDeleteAccount?.(account.id);
                            }}
                          >
                            Delete
                          </Button>
                        </div>
                      ) : (
                        <Button
                          size="xs"
                          variant="ghost"
                          className="text-muted-foreground hover:text-destructive"
                          disabled={isMutating}
                          aria-label={`Delete account ${account.id}`}
                          onClick={() => setConfirmingId(account.id)}
                        >
                          <Trash2 size={14} />
                        </Button>
                      )
                    ) : null}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}
