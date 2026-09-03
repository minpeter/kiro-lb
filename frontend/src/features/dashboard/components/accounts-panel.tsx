import { useEffect, useRef, useState } from "react";
import { AtSign, Ban, Check, Copy, KeyRound, PauseCircle, PlayCircle, ServerCog, Trash2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { EmptyState } from "@/components/empty-state";
import { Progress } from "@/components/ui/progress";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { cn } from "@/lib/utils";
import { formatTimestamp, formatUsage } from "../format";
import { resetHint, usageIndicatorClass } from "../quota-display";
import type { Account, AccountRoutingState } from "../types";
import { TableSkeleton } from "./skeletons";

/**
 * "quota exhausted" and "quota spent" are deliberately near-identical: the same
 * condition, reached by different evidence. Exhausted means the upstream refused
 * with a 402; spent means the usage poll reports the allowance gone. Both exclude
 * the account, so both read as an exclusion rather than one looking milder.
 */
const ROUTING_STATE_LABEL: Record<AccountRoutingState, string> = {
  available: "ready",
  rate_limited: "rate limited",
  quota_exhausted: "quota exhausted",
  quota_depleted: "quota spent",
  cooling_down: "cooling down",
  suspended: "BANNED",
  auth_dead: "AUTH DEAD",
  disabled: "disabled",
  uninitialized: "pending",
};

/** Only "ready" is a routing target; everything else is currently excluded. */
function RoutingStateCell({ account }: { account: Account }) {
  const state = account.routingState;
  const label = ROUTING_STATE_LABEL[state] ?? state;
  const variant = state === "available" ? "secondary" : state === "uninitialized" ? "outline" : "destructive";
  const suspended = state === "suspended";
  const authDead = state === "auth_dead";
  // The one reset display for the row: the countdown from the router, stated
  // here and nowhere else.
  const hint = resetHint(account);
  return (
    <div className="space-y-1">
      <Badge variant={variant} className={suspended || authDead ? "font-semibold tracking-wide" : undefined}>
        {suspended && <Ban size={12} />}
        {authDead && <KeyRound size={12} />}
        {label}
      </Badge>
      {authDead ? (
        // Names the remedy rather than the symptom: unlike a suspension, this one
        // is fixed by the operator re-registering the account.
        <p className="text-xs text-destructive">credential rejected; re-login required</p>
      ) : suspended ? (
        <p className="text-xs text-destructive">locked by Kiro; contact support</p>
      ) : (
        hint && <p className="text-xs tabular-nums text-muted-foreground">{hint}</p>
      )}
    </div>
  );
}

/**
 * The hashed credential label, clickable to copy. The id is what client-facing
 * 503 diagnostics and metrics name, so an operator reading a log line needs it
 * on the clipboard, not just on screen. The full id stays in the tooltip and
 * aria-label while the visible text truncates with the cell.
 */
function CopyableAccountId({ id, className }: { id: string; className?: string }) {
  const [copied, setCopied] = useState(false);
  const copyTimer = useRef<number | null>(null);

  useEffect(
    () => () => {
      if (copyTimer.current !== null) window.clearTimeout(copyTimer.current);
    },
    [],
  );

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(id);
      setCopied(true);
      if (copyTimer.current !== null) window.clearTimeout(copyTimer.current);
      copyTimer.current = window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard unavailable: the id stays visible for manual selection.
    }
  };

  return (
    <button
      type="button"
      onClick={copy}
      title={id}
      aria-label={copied ? `Copied account id ${id}` : `Copy account id ${id}`}
      className={cn(
        "group inline-flex max-w-full cursor-pointer items-center gap-1 font-mono text-xs hover:text-foreground",
        className,
      )}
    >
      {copied ? (
        <Check size={12} className="shrink-0 text-success" />
      ) : (
        <Copy size={12} className="shrink-0 opacity-40 transition-opacity group-hover:opacity-70" />
      )}
      <span className="truncate">{id}</span>
    </button>
  );
}

/**
 * Identity of one pool account: the upstream email when the quota poll has
 * reported it, with the hashed credential label underneath. The label stays
 * visible either way because it is what client-facing 503 diagnostics name.
 */
function AccountCell({ account }: { account: Account }) {
  const email = account.usage?.email;
  if (!email) return <CopyableAccountId id={account.id} />;
  return (
    <div className="group flex min-w-0 flex-col gap-0.5">
      <span className="flex min-w-0 items-center gap-1.5 font-medium" title={email}>
        <AtSign size={13} className="shrink-0 text-muted-foreground" />
        <span className="truncate">{email}</span>
      </span>
      <CopyableAccountId id={account.id} className="pl-[1.15rem] text-muted-foreground" />
    </div>
  );
}

/**
 * A failed poll reports why, without being able to resize the table.
 *
 * The message is upstream text of unbounded length: an httpx status error is 188
 * characters across two lines, and rendering it bare into a `whitespace-nowrap`
 * cell forced the row wider than the viewport and pushed every later column
 * off-screen. The backend now summarizes these, but the cell must not depend on
 * that: a width cap plus clamped wrapping makes the layout hold for any string,
 * and the full text stays available through the `title` tooltip rather than
 * being truncated away.
 */
function UsageErrorCell({ message }: { message: string }) {
  return (
    <p
      title={message}
      className="line-clamp-2 max-w-40 text-xs break-words whitespace-normal text-destructive"
    >
      {message}
    </p>
  );
}

function UsageCell({ account }: { account: Account }) {
  const usage = account.usage;
  // A failed poll keeps the previous figures, so showing the error instead of
  // them hides information that is still useful. The error becomes a warning
  // above the bar, and only replaces it when there is nothing to show.
  const percent = usage?.usagePercent;
  if (usage?.error && percent == null) return <UsageErrorCell message={usage.error} />;
  if (!usage || percent == null) return <span className="text-muted-foreground">—</span>;
  return (
    <div className="min-w-40 space-y-1.5">
      {usage.error && (
        <p title={usage.error} className="line-clamp-1 text-xs text-warning">
          last check failed
        </p>
      )}
      <Progress
        value={Math.min(percent, 100)}
        className="h-1.5"
        indicatorClassName={usageIndicatorClass(percent)}
      />
      <p className="text-xs tabular-nums text-muted-foreground">{formatUsage(usage)}</p>
    </div>
  );
}

export type AccountsPanelProps = {
  accounts: Account[];
  isLoading: boolean;
  isMutating?: boolean;
  onDeleteAccount?: (id: string) => void;
  onToggleAccount?: (id: string, enabled: boolean) => void;
};

export function AccountsPanel({ accounts, isLoading, isMutating, onDeleteAccount, onToggleAccount }: AccountsPanelProps) {
  const [deleting, setDeleting] = useState<Account | null>(null);
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
          <TableSkeleton rows={2} columns={9} />
        ) : accounts.length === 0 ? (
          <EmptyState icon={ServerCog} title="No accounts registered" description="Add a credential source below." />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Account</TableHead>
                <TableHead>State</TableHead>
                {/* Low-value columns drop below md so Account/State/Usage fit a phone viewport. */}
                <TableHead className="hidden md:table-cell">Plan</TableHead>
                <TableHead className="hidden md:table-cell">Overage</TableHead>
                <TableHead>Usage</TableHead>
                <TableHead className="text-right">Requests</TableHead>
                <TableHead className="text-right">Failures</TableHead>
                <TableHead className="hidden md:table-cell">Updated</TableHead>
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
                  <TableCell className="hidden md:table-cell">{account.usage?.subscriptionTitle ?? "—"}</TableCell>
                  <TableCell className="hidden md:table-cell">
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
                  <TableCell className="text-right tabular-nums">{account.requests.toLocaleString()}</TableCell>
                  <TableCell className="text-right tabular-nums">{account.failures.toLocaleString()}</TableCell>
                  <TableCell className="hidden text-xs text-muted-foreground md:table-cell">
                    {formatTimestamp(account.usage?.updatedAt)}
                  </TableCell>
                  <TableCell className="text-right">
                    <div className="flex items-center justify-end gap-1">
                      {onToggleAccount && account.enabled !== undefined ? (
                        <Button
                          size="xs"
                          variant="ghost"
                          className="text-muted-foreground hover:text-foreground"
                          disabled={isMutating}
                          title={
                            account.enabled
                              ? "Stop routing to this account, keeping it and its history"
                              : "Put this account back in the rotation"
                          }
                          aria-label={`${account.enabled ? "Disable" : "Enable"} account ${account.id}`}
                          onClick={() => onToggleAccount(account.id, !account.enabled)}
                        >
                          {account.enabled ? <PauseCircle size={14} /> : <PlayCircle size={14} />}
                        </Button>
                      ) : null}
                      {account.deletable ? (
                        <Button
                          size="xs"
                          variant="ghost"
                          className="text-muted-foreground hover:text-destructive"
                          disabled={isMutating}
                          title="Delete"
                          aria-label={`Delete account ${account.id}`}
                          onClick={() => setDeleting(account)}
                        >
                          <Trash2 size={14} />
                        </Button>
                      ) : null}
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>

      <Dialog open={deleting !== null} onOpenChange={(open) => !open && setDeleting(null)}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Delete this account?</DialogTitle>
            <DialogDescription>
              {deleting ? (
                <>
                  Account <span className="font-mono">{deleting.id}</span> and its usage history are removed.
                  To stop using it without losing it, use the pause button instead.
                </>
              ) : null}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" disabled={isMutating} onClick={() => setDeleting(null)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={isMutating}
              onClick={() => {
                if (deleting) onDeleteAccount?.(deleting.id);
                setDeleting(null);
              }}
            >
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}
