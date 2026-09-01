import { Activity, HeartPulse, Server, Users } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableRow } from "@/components/ui/table";
import { formatTimestamp } from "../format";
import type { Account, Overview } from "../types";

export type InfoPanelProps = {
  overview?: Overview;
  accounts: Account[];
  routableAccounts?: number;
  lastUpdatedAt?: number;
  isLive: boolean;
};

function uptime(seconds?: number): string {
  if (!seconds) return "—";
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <TableRow>
      <TableCell className="w-1/2 text-muted-foreground">{label}</TableCell>
      <TableCell className="text-right font-medium">{value}</TableCell>
    </TableRow>
  );
}

export function InfoPanel({ overview, accounts, routableAccounts, lastUpdatedAt, isLive }: InfoPanelProps) {
  const enabled = accounts.filter((account) => account.enabled !== false);
  const disabled = accounts.length - enabled.length;
  const suspended = enabled.filter((account) => account.routingState === "suspended").length;
  const cooling = enabled.filter((account) => account.routingState === "cooling_down").length;
  const rateLimited = enabled.filter((account) => account.routingState === "rate_limited").length;

  return (
    <div className="grid gap-6 xl:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <HeartPulse size={16} aria-hidden /> Service
          </CardTitle>
          <CardDescription>How this gateway process is doing right now.</CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableBody>
              <Row
                label="Status"
                value={
                  <Badge
                    variant={overview?.proxy.status === "healthy" ? "outline" : "destructive"}
                    className={
                      overview?.proxy.status === "healthy" ? "border-success/40 text-success" : undefined
                    }
                  >
                    {overview?.proxy.status ?? "unknown"}
                  </Badge>
                }
              />
              <Row label="Uptime" value={uptime(overview?.proxy.uptimeSeconds)} />
              <Row
                label="Average latency"
                value={overview ? `${Math.round(overview.averageLatencyMs)} ms` : "—"}
              />
              <Row label="Live updates" value={isLive ? "on" : "paused"} />
              <Row label="Last update" value={formatTimestamp(lastUpdatedAt ? lastUpdatedAt / 1000 : undefined)} />
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Users size={16} aria-hidden /> Account pool
          </CardTitle>
          <CardDescription>What the router has to work with.</CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableBody>
              <Row label="Routable now" value={`${routableAccounts ?? 0} of ${enabled.length}`} />
              <Row label="Initialized" value={`${overview?.accounts.initialized ?? 0} of ${overview?.accounts.total ?? 0}`} />
              <Row label="Disabled" value={disabled} />
              <Row
                label="Suspended by Kiro"
                value={suspended ? <span className="text-destructive">{suspended}</span> : 0}
              />
              <Row label="Cooling down" value={cooling} />
              <Row label="Rate limited" value={rateLimited} />
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Activity size={16} aria-hidden /> Traffic (24h)
          </CardTitle>
          <CardDescription>Counted from the request log.</CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableBody>
              <Row label="Requests" value={(overview?.requests24h ?? 0).toLocaleString()} />
              <Row label="Successful" value={(overview?.successes24h ?? 0).toLocaleString()} />
              <Row
                label="Failed"
                value={Math.max(0, (overview?.requests24h ?? 0) - (overview?.successes24h ?? 0)).toLocaleString()}
              />
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Server size={16} aria-hidden /> Models
          </CardTitle>
          <CardDescription>Reported by the accounts in the pool.</CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableBody>
              <Row label="Available" value={overview?.models ?? 0} />
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}
