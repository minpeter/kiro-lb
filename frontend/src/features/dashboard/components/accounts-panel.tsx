import { ServerCog } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/empty-state";
import { Progress } from "@/components/ui/progress";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatTimestamp, formatUsage } from "../format";
import type { Account } from "../types";
import { TableSkeleton } from "./skeletons";

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

export function AccountsPanel({ accounts, isLoading }: { accounts: Account[]; isLoading: boolean }) {
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
          <TableSkeleton rows={2} columns={6} />
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
              </TableRow>
            </TableHeader>
            <TableBody>
              {accounts.map((account) => (
                <TableRow key={account.id}>
                  <TableCell className="font-mono text-xs">{account.id}</TableCell>
                  <TableCell>
                    <Badge variant={account.initialized ? "secondary" : "outline"}>
                      {account.initialized ? "ready" : "pending"}
                    </Badge>
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
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}
