import { useState } from "react";
import { ChevronDown, ChevronRight, KeyRound, ShieldCheck } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/empty-state";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatTimestamp } from "../format";
import type { ApiKey, KeyModelUsage, KeyUsage } from "../types";
import { TableSkeleton } from "./skeletons";

export type ApiKeysPanelProps = {
  apiKeys: ApiKey[];
  keyUsage: KeyUsage;
  isLoading: boolean;
  isMutating: boolean;
  onCreate: () => void;
  onRevoke: (id: string) => void;
};

const compact = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 });

function totals(rows: KeyModelUsage[]) {
  return rows.reduce(
    (sum, row) => ({
      tokens: sum.tokens + row.totalTokens,
      requests: sum.requests + row.requests,
    }),
    { tokens: 0, requests: 0 },
  );
}

function UsageBreakdown({ rows }: { rows: KeyModelUsage[] }) {
  if (rows.length === 0) {
    return <p className="px-4 py-3 text-xs text-muted-foreground">No traffic recorded for this key yet.</p>;
  }
  return (
    <div className="bg-muted/40 px-4 py-3">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Model</TableHead>
            <TableHead className="text-right">Requests</TableHead>
            <TableHead className="text-right">Input</TableHead>
            <TableHead className="text-right">Output</TableHead>
            <TableHead className="text-right">Total</TableHead>
            <TableHead>Last used</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((row) => (
            <TableRow key={row.model}>
              <TableCell className="font-mono text-xs">{row.model}</TableCell>
              <TableCell className="text-right tabular-nums">{row.requests.toLocaleString()}</TableCell>
              <TableCell className="text-right tabular-nums">{row.promptTokens.toLocaleString()}</TableCell>
              <TableCell className="text-right tabular-nums">{row.completionTokens.toLocaleString()}</TableCell>
              <TableCell className="text-right font-medium tabular-nums">{row.totalTokens.toLocaleString()}</TableCell>
              <TableCell className="text-xs text-muted-foreground">{formatTimestamp(row.updatedAt)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

export function ApiKeysPanel({ apiKeys, keyUsage, isLoading, isMutating, onCreate, onRevoke }: ApiKeysPanelProps) {
  const [expanded, setExpanded] = useState<string | null>(null);

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="space-y-1">
            <CardTitle>API keys</CardTitle>
            <CardDescription>
              scrypt-hashed; the plaintext value is shown only once at creation. Select a key to see its token usage per model.
            </CardDescription>
          </div>
          <Button size="sm" disabled={isMutating} onClick={onCreate}>
            <KeyRound />
            Create key
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <TableSkeleton rows={3} columns={6} />
        ) : apiKeys.length === 0 ? (
          <EmptyState icon={KeyRound} title="No API keys" description="Create a key to authenticate /v1 clients." />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-8" />
                <TableHead>Name</TableHead>
                <TableHead>Prefix</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="text-right">Requests</TableHead>
                <TableHead className="text-right">Tokens</TableHead>
                <TableHead>Created</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {apiKeys.map((key) => {
                const rows = keyUsage[key.id] ?? [];
                const { tokens, requests } = totals(rows);
                const isOpen = expanded === key.id;
                return [
                  <TableRow
                    key={key.id}
                    className="cursor-pointer"
                    onClick={() => setExpanded(isOpen ? null : key.id)}
                  >
                    <TableCell className="text-muted-foreground">
                      {isOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                    </TableCell>
                    <TableCell>
                      <span className="flex items-center gap-1.5">
                        {key.readOnly && <ShieldCheck size={13} className="text-muted-foreground" />}
                        {key.name}
                      </span>
                    </TableCell>
                    <TableCell className="font-mono text-xs">{key.prefix}…</TableCell>
                    <TableCell>
                      <Badge variant={key.readOnly ? "default" : key.revokedAt ? "outline" : "secondary"}>
                        {key.readOnly ? "root" : key.revokedAt ? "revoked" : "active"}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{requests ? requests.toLocaleString() : "—"}</TableCell>
                    <TableCell className="text-right tabular-nums">{tokens ? compact.format(tokens) : "—"}</TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {key.readOnly ? "environment" : formatTimestamp(key.createdAt)}
                    </TableCell>
                    <TableCell className="text-right">
                      {key.readOnly || key.revokedAt ? null : (
                        <Button
                          size="xs"
                          variant="outline"
                          disabled={isMutating}
                          onClick={(event) => {
                            event.stopPropagation();
                            onRevoke(key.id);
                          }}
                        >
                          Revoke
                        </Button>
                      )}
                    </TableCell>
                  </TableRow>,
                  isOpen ? (
                    <TableRow key={`${key.id}-usage`}>
                      <TableCell colSpan={8} className="p-0">
                        <UsageBreakdown rows={rows} />
                      </TableCell>
                    </TableRow>
                  ) : null,
                ];
              })}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}
