import { KeyRound } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/empty-state";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatTimestamp } from "../format";
import type { ApiKey } from "../types";
import { TableSkeleton } from "./skeletons";

export type ApiKeysPanelProps = {
  apiKeys: ApiKey[];
  isLoading: boolean;
  isMutating: boolean;
  onCreate: () => void;
  onRevoke: (id: string) => void;
};

export function ApiKeysPanel({ apiKeys, isLoading, isMutating, onCreate, onRevoke }: ApiKeysPanelProps) {
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="space-y-1">
            <CardTitle>API keys</CardTitle>
            <CardDescription>scrypt-hashed; the plaintext value is shown only once at creation.</CardDescription>
          </div>
          <Button size="sm" disabled={isMutating} onClick={onCreate}>
            <KeyRound />
            Create key
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <TableSkeleton rows={3} columns={4} />
        ) : apiKeys.length === 0 ? (
          <EmptyState
            icon={KeyRound}
            title="No dashboard-managed keys"
            description="The legacy PROXY_API_KEY still works for clients."
          />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Prefix</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Created</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {apiKeys.map((key) => (
                <TableRow key={key.id}>
                  <TableCell>{key.name}</TableCell>
                  <TableCell className="font-mono text-xs">{key.prefix}…</TableCell>
                  <TableCell>
                    <Badge variant={key.revokedAt ? "outline" : "secondary"}>{key.revokedAt ? "revoked" : "active"}</Badge>
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">{formatTimestamp(key.createdAt)}</TableCell>
                  <TableCell className="text-right">
                    {key.revokedAt ? null : (
                      <Button size="xs" variant="outline" disabled={isMutating} onClick={() => onRevoke(key.id)}>
                        Revoke
                      </Button>
                    )}
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
