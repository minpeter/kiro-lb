import { Fragment, useEffect, useRef, useState } from "react";
import { Popover } from "radix-ui";
import { Ban, Check, Clock3, Copy, ExternalLink, Info, KeyRound, PauseCircle, PlayCircle, ServerCog, ShieldAlert, Trash2, Users, X } from "lucide-react";
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
import { formatDuration, formatTimestamp, formatUsage } from "../format";
import { usageIndicatorClass } from "../quota-display";
import { usePreferences } from "../preferences";
import type { Account, AccountRoutingState } from "../types";
import { groupAccounts } from "./account-groups";
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
  suspended: "Temporary suspension",
  account_issue: "AWS account issue",
  auth_dead: "AUTH DEAD",
  disabled: "disabled",
  uninitialized: "pending",
};

function TokenHubBadge({ dashboardUrl }: { dashboardUrl?: string | null }) {
  const { t } = usePreferences();
  const brand = <>
    {/* Official mark from ino-tokenhub/apps/dashboard/public/brand/token-hub.svg. */}
    <svg viewBox="0 0 64 64" fill="currentColor" aria-hidden="true" className="size-4!">
      <path d="M7 17 27 5V23L19 30V59L7 51ZM45 7 57 15V51L37 61V45L45 37ZM29 25 41 33 43 37 35 43H33L21 35V31L27 25Z" />
    </svg>
    <span>Token Hub</span>
  </>;
  if (!dashboardUrl) return (
    <Badge variant="outline" className="min-h-8 gap-1.5" title={t("accounts.tokenHubNotConfigured")}>
      {brand}
    </Badge>
  );
  return (
    <Badge asChild variant="outline" className="min-h-8 gap-1.5 motion-reduce:transition-none">
      <a href={dashboardUrl} target="_blank" rel="noopener noreferrer" aria-label={t("accounts.openTokenHub")} title={t("accounts.openTokenHub")}>
        {brand}<ExternalLink className="opacity-60" aria-hidden="true" />
      </a>
    </Badge>
  );
}

/** Only "ready" is a routing target; everything else is currently excluded. */
function RoutingStateCell({ account, tokenHubDashboardUrl }: {
  account: Account;
  tokenHubDashboardUrl?: string | null;
}) {
  const { t } = usePreferences();
  const state = account.routingState;
  const [open, setOpen] = useState(false);
  const pinned = useRef(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const restoringFocus = useRef(false);
  const returnFocusOnClose = useRef(false);
  const cancelTimer = () => { if (timer.current) clearTimeout(timer.current); };
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);
  const label = ROUTING_STATE_LABEL[state] ? t(`accounts.state.${state}`) : state;
  const variant = state === "available" ? "secondary" : state === "uninitialized" || state === "disabled" ? "outline" : "destructive";
  const suspended = state === "suspended";
  const authDead = state === "auth_dead";
  const accountIssue = state === "account_issue";
  const hasDetails = accountIssue || authDead || suspended;
  // The one reset display for the row: the countdown from the router, stated
  // here and nowhere else.
  const quotaGone = state === "quota_exhausted" || state === "quota_depleted";
  const eta = account.eligibleInSeconds > 0 ? formatDuration(account.eligibleInSeconds) : null;
  const hint = quotaGone
    ? eta
      ? t("accounts.resetsIn", { d: eta })
      : t("accounts.untilReset")
    : eta
      ? t("accounts.backIn", { d: eta })
      : null;
  const badge = (
      <>
        {suspended && <Clock3 size={12} />}
        {state === "account_issue" && <ShieldAlert size={12} />}
        {authDead && <KeyRound size={12} />}
        {label}
      </>
  );
  if (!hasDetails) return (
    <div className="space-y-1">
      <Badge variant={variant}>{badge}</Badge>
      {hint && <p className="text-xs tabular-nums text-muted-foreground">{hint}</p>}
    </div>
  );
  const closeAfterHover = () => {
    cancelTimer();
    timer.current = setTimeout(() => {
      if (!pinned.current && !content.current?.contains(document.activeElement) && !trigger.current?.matches(":focus-visible")) setOpen(false);
    }, 180);
  };
  return (
    <Popover.Root open={open} onOpenChange={(next) => { cancelTimer(); if (!next) pinned.current = false; setOpen(next); }}>
      <Popover.Trigger asChild>
        <Badge asChild variant={variant} className="cursor-pointer gap-1.5 outline-none hover:brightness-110 motion-reduce:transition-none">
          <button ref={trigger} type="button" aria-label={t("accounts.statusDetails", { state: label, id: account.id })}
            onPointerEnter={(event) => {
              if (event.pointerType !== "mouse") return;
              cancelTimer(); timer.current = setTimeout(() => setOpen(true), 150);
            }}
            onPointerLeave={closeAfterHover}
            onFocus={() => { if (!restoringFocus.current && trigger.current?.matches(":focus-visible")) { cancelTimer(); setOpen(true); } }}
            onClick={(event) => { event.preventDefault(); cancelTimer(); pinned.current = true; setOpen(true); }}
            onKeyDown={(event) => {
              if (event.key === "ArrowDown" && open) { event.preventDefault(); content.current?.focus(); }
            }}
          >{badge}<Info size={12} className="opacity-65" aria-hidden /></button>
        </Badge>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content ref={content} side="bottom" align="start" sideOffset={8} collisionPadding={16}
          aria-label={t("accounts.statusDetails", { state: label, id: account.id })} tabIndex={-1}
          onPointerEnter={cancelTimer} onPointerLeave={closeAfterHover}
          onFocusCapture={() => { returnFocusOnClose.current = true; }}
          onInteractOutside={() => { returnFocusOnClose.current = false; }}
          onOpenAutoFocus={(event) => event.preventDefault()}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            if (returnFocusOnClose.current) {
              returnFocusOnClose.current = false;
              restoringFocus.current = true;
              trigger.current?.focus();
              queueMicrotask(() => { restoringFocus.current = false; });
            }
          }}
          className="z-50 w-80 max-w-[calc(100vw-2rem)] space-y-3 rounded-lg border bg-popover p-4 text-popover-foreground shadow-lg outline-none data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=open]:fade-in-0 data-[state=closed]:fade-out-0 data-[state=open]:zoom-in-95 data-[state=closed]:zoom-out-95 duration-150 motion-reduce:animate-none!"
        >
          <div className="flex items-center justify-between gap-3">
            <p className="text-sm font-semibold">{t(accountIssue ? "accounts.state.account_issue" : `accounts.state.${state}`)}</p>
            <Popover.Close asChild><Button size="icon-xs" variant="ghost" aria-label={t("accounts.closeDetails")}><X /></Button></Popover.Close>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <p className="w-fit rounded border bg-muted/40 px-2 py-1 font-mono text-[11px]">{accountIssue ? "ERR-837" : suspended ? "TEMPORARILY_SUSPENDED" : "AUTH DEAD"}</p>
            {(accountIssue || account.awsLoginDiagnostic) && <TokenHubBadge dashboardUrl={tokenHubDashboardUrl} />}
          </div>
          <p className="text-sm leading-relaxed text-muted-foreground">{t(accountIssue ? "accounts.accountIssueHint" : suspended ? "accounts.suspendedHint" : "accounts.authDeadHint")}</p>
          {!!account.awsLoginIssueAt && <p className="text-xs text-muted-foreground">{t("accounts.lastConfirmedAt", { at: formatTimestamp(account.awsLoginIssueAt) })}</p>}
          {account.awsLoginDiagnostic && !(account.awsLoginDiagnostic.result === "ERR-837" && account.awsLoginDiagnostic.checkedAt === account.awsLoginIssueAt) && (
            <div className="space-y-1 border-t pt-3 text-xs text-muted-foreground">
              <p>{t(`accounts.diagnostic.${account.awsLoginDiagnostic.result}`)}</p>
              <p>{t("accounts.lastCheckedAt", { at: formatTimestamp(account.awsLoginDiagnostic.checkedAt) })}</p>
            </div>
          )}
          <Popover.Arrow className="fill-popover" />
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}

/**
 * The hashed credential label, clickable to copy. The id is what client-facing
 * 503 diagnostics and metrics name, so an operator reading a log line needs it
 * on the clipboard, not just on screen. The full id stays in the tooltip and
 * aria-label while the visible text truncates with the cell.
 */
function CopyableAccountId({ id, className }: { id: string; className?: string }) {
  const { t } = usePreferences();
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
      aria-label={copied ? t("accounts.copiedId", { id }) : t("accounts.copyId", { id })}
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
      <span className="block truncate font-medium" title={email}>{email}</span>
      <CopyableAccountId id={account.id} className="text-muted-foreground" />
    </div>
  );
}

/** Keep unbounded upstream errors in the tooltip, not in the table layout. */
function UsageErrorCell({ message }: { message: string }) {
  const { t } = usePreferences();
  return (
    <p
      title={message}
      className="line-clamp-2 max-w-40 text-xs break-words whitespace-normal text-muted-foreground"
    >
      {t("accounts.usageUnavailable")}
    </p>
  );
}

function UsageCell({ account }: { account: Account }) {
  const { t } = usePreferences();
  const usage = account.usage;
  // Keep the last known figures, but distinguish them from a fresh reading.
  // Account failures belong in the state column; usage only describes the data.
  const percent = usage?.usagePercent;
  if (usage?.error && percent == null) return <UsageErrorCell message={usage.error} />;
  if (!usage || percent == null) return <span className="text-muted-foreground">—</span>;
  return (
    <div className="min-w-40 space-y-1.5">
      <Progress
        value={Math.min(percent, 100)}
        className="h-1.5"
        indicatorClassName={usageIndicatorClass(percent)}
      />
      <p className="flex flex-wrap gap-x-1 text-xs tabular-nums text-muted-foreground">
        <span>{formatUsage(usage)}</span>
        {usage.error && <span title={usage.error}>· {t("accounts.previousUsage")}</span>}
      </p>
    </div>
  );
}

function AccountCard({
  account,
  tokenHubDashboardUrl,
  isMutating,
  onDelete,
  onToggle,
}: {
  account: Account;
  tokenHubDashboardUrl?: string | null;
  isMutating?: boolean;
  onDelete: (account: Account) => void;
  onToggle?: (id: string, enabled: boolean) => void;
}) {
  const { t } = usePreferences();
  const overage = account.usage?.overageStatus;
  return (
    <article className={cn("space-y-4 rounded-lg border p-4", (account.enabled === false || account.routingState !== "available") && "bg-muted/15 text-muted-foreground")}>
      <div className="flex min-w-0 flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 max-w-full flex-1"><AccountCell account={account} /></div>
        <RoutingStateCell account={account} tokenHubDashboardUrl={tokenHubDashboardUrl} />
      </div>
      <UsageCell account={account} />
      <dl className="grid grid-cols-2 gap-x-4 gap-y-3 text-sm sm:grid-cols-3">
        <div className="min-w-0">
          <dt className="text-xs text-muted-foreground">{t("accounts.col.plan")}</dt>
          <dd className="truncate text-foreground">{account.usage?.subscriptionTitle ?? "—"}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">{t("accounts.col.overage")}</dt>
          <dd className="text-foreground">
            {overage == null || overage === "UNKNOWN"
              ? "—"
              : overage === "DISABLED"
                ? t("accounts.overage.disabled")
                : overage === "ENABLED"
                  ? t("accounts.overage.enabled")
                  : overage}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">{t("accounts.col.requests")}</dt>
          <dd className="tabular-nums text-foreground">{account.requests.toLocaleString()}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">{t("accounts.col.failures")}</dt>
          <dd className="tabular-nums text-foreground">{account.failures.toLocaleString()}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">{t("accounts.col.sessions")}</dt>
          <dd className="tabular-nums text-foreground">{(account.sessions ?? 0).toLocaleString()}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">{t("accounts.col.updated")}</dt>
          <dd className="text-xs text-foreground">{formatTimestamp(account.usage?.updatedAt)}</dd>
        </div>
      </dl>
      <div className="flex items-center justify-end gap-2 border-t pt-3">
        {onToggle && account.enabled !== undefined && (
          <Button
            size="sm"
            variant="outline"
            disabled={isMutating}
            onClick={() => onToggle(account.id, !account.enabled)}
          >
            {account.enabled ? <PauseCircle /> : <PlayCircle />}
            {t(account.enabled ? "accounts.pause" : "accounts.resume")}
          </Button>
        )}
        {account.deletable && (
          <Button size="sm" variant="ghost" className="text-muted-foreground hover:text-destructive" disabled={isMutating} onClick={() => onDelete(account)}>
            <Trash2 /> {t("accounts.delete")}
          </Button>
        )}
      </div>
    </article>
  );
}

export type AccountsPanelProps = {
  accounts: Account[];
  tokenHubDashboardUrl?: string | null;
  isLoading: boolean;
  isMutating?: boolean;
  onDeleteAccount?: (id: string) => void;
  onToggleAccount?: (id: string, enabled: boolean) => void;
};

export function AccountsPanel({ accounts, tokenHubDashboardUrl, isLoading, isMutating, onDeleteAccount, onToggleAccount }: AccountsPanelProps) {
  const { t } = usePreferences();
  const [deleting, setDeleting] = useState<Account | null>(null);
  const { activeAccounts, unavailableAccounts, pausedAccounts, authDeadAccounts, bannedAccounts, accountIssueAccounts, displayedAccounts } = groupAccounts(accounts);
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Users size={16} aria-hidden /> {t("accounts.title")}
        </CardTitle>
        <CardDescription>{t("accounts.description")}</CardDescription>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <TableSkeleton rows={2} columns={9} />
        ) : accounts.length === 0 ? (
          <EmptyState icon={ServerCog} title={t("accounts.emptyTitle")} description={t("accounts.emptyDescription")} />
        ) : (
          <>
            <div className="space-y-3 lg:hidden">
              {activeAccounts.map((account) => (
                <AccountCard key={account.id} account={account} tokenHubDashboardUrl={tokenHubDashboardUrl} isMutating={isMutating} onDelete={setDeleting} onToggle={onToggleAccount} />
              ))}
              {unavailableAccounts.length > 0 && (
                <div className="space-y-3 pt-2">
                  <div className="rounded-md bg-muted/30 px-3 py-2">
                    <p className="flex items-center gap-2 font-medium">
                      <Clock3 size={14} aria-hidden="true" />
                      {t("accounts.unavailableSection")}
                    </p>
                    <p className="text-xs break-keep text-muted-foreground">{t("accounts.unavailableDescription")}</p>
                  </div>
                  {unavailableAccounts.map((account) => (
                    <AccountCard key={account.id} account={account} tokenHubDashboardUrl={tokenHubDashboardUrl} isMutating={isMutating} onDelete={setDeleting} onToggle={onToggleAccount} />
                  ))}
                </div>
              )}
              {pausedAccounts.length > 0 && (
                <div className="space-y-3 pt-2">
                  <div className="rounded-md bg-muted/30 px-3 py-2">
                    <p className="font-medium">{t("accounts.pausedSection")}</p>
                    <p className="text-xs break-keep text-muted-foreground">{t("accounts.pausedDescription")}</p>
                  </div>
                  {pausedAccounts.map((account) => (
                    <AccountCard key={account.id} account={account} tokenHubDashboardUrl={tokenHubDashboardUrl} isMutating={isMutating} onDelete={setDeleting} onToggle={onToggleAccount} />
                  ))}
                </div>
              )}
              {authDeadAccounts.length > 0 && (
                <div className="space-y-3 pt-2">
                  <div className="rounded-md bg-destructive/5 px-3 py-2">
                    <p className="flex items-center gap-2 font-medium text-destructive">
                      <KeyRound size={14} aria-hidden="true" />
                      {t("accounts.authDeadSection")}
                    </p>
                    <p className="text-xs break-keep text-muted-foreground">{t("accounts.authDeadDescription")}</p>
                  </div>
                  {authDeadAccounts.map((account) => (
                    <AccountCard key={account.id} account={account} tokenHubDashboardUrl={tokenHubDashboardUrl} isMutating={isMutating} onDelete={setDeleting} onToggle={onToggleAccount} />
                  ))}
                </div>
              )}
              {bannedAccounts.length > 0 && (
                <div className="space-y-3 pt-2">
                  <div className="rounded-md bg-destructive/5 px-3 py-2">
                    <p className="flex items-center gap-2 font-medium text-destructive">
                      <Ban size={14} aria-hidden="true" />
                      {t("accounts.bannedSection")}
                    </p>
                    <p className="text-xs break-keep text-muted-foreground">{t("accounts.bannedDescription")}</p>
                  </div>
                  {bannedAccounts.map((account) => (
                    <AccountCard key={account.id} account={account} tokenHubDashboardUrl={tokenHubDashboardUrl} isMutating={isMutating} onDelete={setDeleting} onToggle={onToggleAccount} />
                  ))}
                </div>
              )}
              {accountIssueAccounts.length > 0 && (
                <div className="space-y-3 pt-2">
                  <div className="rounded-md bg-destructive/5 px-3 py-2">
                    <div className="mb-1 flex flex-wrap items-center justify-between gap-2">
                      <p className="flex items-center gap-2 font-medium text-destructive">
                        <ShieldAlert size={14} aria-hidden="true" />{t("accounts.accountIssueSection")}
                      </p>
                      <TokenHubBadge dashboardUrl={tokenHubDashboardUrl} />
                    </div>
                    <p className="text-xs break-keep text-muted-foreground">{t("accounts.accountIssueDescription")}</p>
                  </div>
                  {accountIssueAccounts.map((account) => (
                    <AccountCard key={account.id} account={account} tokenHubDashboardUrl={tokenHubDashboardUrl} isMutating={isMutating} onDelete={setDeleting} onToggle={onToggleAccount} />
                  ))}
                </div>
              )}
            </div>
            <div className="hidden lg:block">
              <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t("accounts.col.account")}</TableHead>
                <TableHead>{t("accounts.col.state")}</TableHead>
                {/* Low-value columns drop below md so Account/State/Usage fit a phone viewport. */}
                <TableHead className="hidden md:table-cell">{t("accounts.col.plan")}</TableHead>
                <TableHead className="hidden md:table-cell">{t("accounts.col.overage")}</TableHead>
                <TableHead>{t("accounts.col.usage")}</TableHead>
                <TableHead className="text-right">{t("accounts.col.requests")}</TableHead>
                <TableHead className="text-right">{t("accounts.col.failures")}</TableHead>
                <TableHead className="hidden text-right md:table-cell">{t("accounts.col.sessions")}</TableHead>
                <TableHead className="hidden md:table-cell">{t("accounts.col.updated")}</TableHead>
                <TableHead className="w-12" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {displayedAccounts.map((account) => (
                <Fragment key={account.id}>
                  {account === unavailableAccounts[0] && (
                    <TableRow className="bg-muted/30 hover:bg-muted/30">
                      <TableCell colSpan={10} className="whitespace-normal py-3">
                        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                          <span className="inline-flex items-center gap-2 font-medium text-foreground">
                            <Clock3 size={14} aria-hidden="true" />
                            {t("accounts.unavailableSection")}
                          </span>
                          <span className="text-xs text-muted-foreground">{t("accounts.unavailableDescription")}</span>
                        </div>
                      </TableCell>
                    </TableRow>
                  )}
                  {account === pausedAccounts[0] && (
                    <TableRow className="bg-muted/30 hover:bg-muted/30">
                      <TableCell colSpan={10} className="whitespace-normal py-3">
                        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                          <span className="font-medium text-foreground">{t("accounts.pausedSection")}</span>
                          <span className="text-xs text-muted-foreground">{t("accounts.pausedDescription")}</span>
                        </div>
                      </TableCell>
                    </TableRow>
                  )}
                  {account === authDeadAccounts[0] && (
                    <TableRow className="bg-destructive/5 hover:bg-destructive/5">
                      <TableCell colSpan={10} className="whitespace-normal py-3">
                        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                          <span className="inline-flex items-center gap-2 font-medium text-destructive">
                            <KeyRound size={14} aria-hidden="true" />
                            {t("accounts.authDeadSection")}
                          </span>
                          <span className="text-xs text-muted-foreground">{t("accounts.authDeadDescription")}</span>
                        </div>
                      </TableCell>
                    </TableRow>
                  )}
                  {account === bannedAccounts[0] && (
                    <TableRow className="bg-destructive/5 hover:bg-destructive/5">
                      <TableCell colSpan={10} className="whitespace-normal py-3">
                        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                          <span className="inline-flex items-center gap-2 font-medium text-destructive">
                            <Ban size={14} aria-hidden="true" />
                            {t("accounts.bannedSection")}
                          </span>
                          <span className="text-xs text-muted-foreground">{t("accounts.bannedDescription")}</span>
                        </div>
                      </TableCell>
                    </TableRow>
                  )}
                  {account === accountIssueAccounts[0] && (
                    <TableRow className="bg-destructive/5 hover:bg-destructive/5">
                      <TableCell colSpan={10} className="whitespace-normal py-3">
                        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                          <span className="inline-flex items-center gap-2 font-medium text-destructive">
                            <ShieldAlert size={14} aria-hidden="true" />{t("accounts.accountIssueSection")}
                          </span>
                          <TokenHubBadge dashboardUrl={tokenHubDashboardUrl} />
                          <span className="text-xs text-muted-foreground">{t("accounts.accountIssueDescription")}</span>
                        </div>
                      </TableCell>
                    </TableRow>
                  )}
                  <TableRow className={account.enabled === false || account.routingState !== "available" ? "bg-muted/15 text-muted-foreground hover:bg-muted/25" : undefined}>
                    <TableCell className="max-w-56">
                      <AccountCell account={account} />
                    </TableCell>
                    <TableCell>
                      <RoutingStateCell account={account} tokenHubDashboardUrl={tokenHubDashboardUrl} />
                    </TableCell>
                    <TableCell className="hidden md:table-cell">{account.usage?.subscriptionTitle ?? "—"}</TableCell>
                    <TableCell className="hidden md:table-cell">
                      {account.usage?.overageStatus == null || account.usage.overageStatus === "UNKNOWN" ? (
                        "—"
                      ) : (
                        <Badge variant={account.usage.overageStatus === "DISABLED" ? "outline" : "secondary"}>
                          {account.usage.overageStatus === "DISABLED"
                            ? t("accounts.overage.disabled")
                            : account.usage.overageStatus === "ENABLED"
                              ? t("accounts.overage.enabled")
                              : account.usage.overageStatus}
                          {account.usage.overageUsed ? ` · ${account.usage.overageUsed.toFixed(2)}` : ""}
                        </Badge>
                      )}
                    </TableCell>
                    <TableCell>
                      <UsageCell account={account} />
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{account.requests.toLocaleString()}</TableCell>
                    <TableCell className="text-right tabular-nums">{account.failures.toLocaleString()}</TableCell>
                    <TableCell className="hidden text-right tabular-nums md:table-cell">
                      {(account.sessions ?? 0).toLocaleString()}
                    </TableCell>
                    <TableCell className="hidden text-xs text-muted-foreground md:table-cell">
                      {formatTimestamp(account.usage?.updatedAt)}
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex items-center justify-end gap-1">
                        {onToggleAccount && account.enabled !== undefined ? (
                          <Button
                            size="xs"
                            variant={account.enabled ? "ghost" : "outline"}
                            className={account.enabled ? "text-muted-foreground hover:text-foreground" : "text-primary hover:text-primary"}
                            disabled={isMutating}
                            title={account.enabled ? t("accounts.disableTitle") : t("accounts.enableTitle")}
                            aria-label={t(account.enabled ? "accounts.disableAria" : "accounts.enableAria", { id: account.id })}
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
                            title={t("accounts.delete")}
                            aria-label={t("accounts.deleteAria", { id: account.id })}
                            onClick={() => setDeleting(account)}
                          >
                            <Trash2 size={14} />
                          </Button>
                        ) : null}
                      </div>
                    </TableCell>
                  </TableRow>
                </Fragment>
              ))}
            </TableBody>
              </Table>
            </div>
          </>
        )}
      </CardContent>

      <Dialog open={deleting !== null} onOpenChange={(open) => !open && setDeleting(null)}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{t("accounts.deleteTitle")}</DialogTitle>
            <DialogDescription>
              {deleting ? (
                <>
                  {t("accounts.deleteBefore")} <span className="font-mono">{deleting.id}</span> {t("accounts.deleteAfter")}
                </>
              ) : null}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" disabled={isMutating} onClick={() => setDeleting(null)}>
              {t("accounts.cancel")}
            </Button>
            <Button
              variant="destructive"
              disabled={isMutating}
              onClick={() => {
                if (deleting) onDeleteAccount?.(deleting.id);
                setDeleting(null);
              }}
            >
              {t("accounts.delete")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}
