import { useEffect, useRef, useState } from "react";
import { Check, ChevronDown, ChevronRight, Copy, KeyRound, Pencil, ShieldCheck, Trash2 } from "lucide-react";
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
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { EmptyState } from "@/components/empty-state";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatKeyPrefix } from "../api-key-display";
import { exactTokens, formatTimestamp, formatTokens } from "../format";
import type { ApiKey, KeyModelUsage, KeyUsage } from "../types";
import { TableSkeleton } from "./skeletons";

export type ApiKeysPanelProps = {
  apiKeys: ApiKey[];
  keyUsage: KeyUsage;
  isLoading: boolean;
  isMutating: boolean;
  onCreate: () => void;
  onDelete: (id: string) => void;
  onRename: (id: string, name: string) => void;
};

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
              <TableCell className="text-right tabular-nums" title={exactTokens(row.promptTokens)}>
                {formatTokens(row.promptTokens)}
              </TableCell>
              <TableCell className="text-right tabular-nums" title={exactTokens(row.completionTokens)}>
                {formatTokens(row.completionTokens)}
              </TableCell>
              <TableCell className="text-right font-medium tabular-nums" title={exactTokens(row.totalTokens)}>
                {formatTokens(row.totalTokens)}
              </TableCell>
              <TableCell className="text-xs text-muted-foreground">{formatTimestamp(row.updatedAt)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

export function ApiKeysPanel({
  apiKeys,
  keyUsage,
  isLoading,
  isMutating,
  onCreate,
  onDelete,
  onRename,
}: ApiKeysPanelProps) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<ApiKey | null>(null);
  const [renaming, setRenaming] = useState<ApiKey | null>(null);
  const [newName, setNewName] = useState("");
  const [copiedPrefixId, setCopiedPrefixId] = useState<string | null>(null);
  const copyTimer = useRef<number | null>(null);

  useEffect(
    () => () => {
      if (copyTimer.current !== null) window.clearTimeout(copyTimer.current);
    },
    [],
  );

  const copyPrefix = async (key: ApiKey) => {
    try {
      await navigator.clipboard.writeText(key.prefix);
      setCopiedPrefixId(key.id);
      if (copyTimer.current !== null) window.clearTimeout(copyTimer.current);
      copyTimer.current = window.setTimeout(() => setCopiedPrefixId(null), 1500);
    } catch {
      // Clipboard unavailable: the prefix stays visible for manual selection.
    }
  };

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
                <TableHead className="hidden text-right md:table-cell">Requests</TableHead>
                <TableHead className="text-right">Tokens</TableHead>
                <TableHead className="hidden md:table-cell">Created</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {apiKeys.map((key) => {
                const rows = keyUsage[key.id] ?? [];
                const { tokens, requests } = totals(rows);
                const isOpen = expanded === key.id;
                const usageRegionId = `api-key-usage-${key.id}`;
                const toggleExpanded = () => setExpanded(isOpen ? null : key.id);
                return [
                  <TableRow
                    key={key.id}
                    className="cursor-pointer focus-visible:bg-accent/50 focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:ring-inset focus-visible:outline-none"
                    tabIndex={0}
                    aria-label={`Usage details for key ${key.name}`}
                    aria-expanded={isOpen}
                    aria-controls={usageRegionId}
                    onClick={toggleExpanded}
                    onKeyDown={(event) => {
                      // Only toggle when the row itself is the target; buttons
                      // inside handle their own activation.
                      if (event.target !== event.currentTarget) return;
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        toggleExpanded();
                      }
                    }}
                  >
                    <TableCell className="text-muted-foreground">
                      <Button
                        variant="ghost"
                        size="icon-xs"
                        tabIndex={-1}
                        aria-hidden
                        // Decorative for AT: the row itself is the focusable disclosure and carries aria-expanded.
                        onClick={(event) => {
                          event.stopPropagation();
                          toggleExpanded();
                        }}
                      >
                        {isOpen ? <ChevronDown /> : <ChevronRight />}
                      </Button>
                    </TableCell>
                    <TableCell>
                      <span className="flex items-center gap-1.5">
                        {key.readOnly && <ShieldCheck size={13} className="text-muted-foreground" />}
                        {key.name}
                        {key.readOnly && <Badge variant="secondary">environment</Badge>}
                      </span>
                    </TableCell>
                    <TableCell>
                      <Button
                        variant="ghost"
                        size="xs"
                        className="h-auto gap-1 px-1 font-mono text-xs font-normal"
                        aria-label={`Copy prefix of key ${key.name}`}
                        title="Copy prefix"
                        onClick={(event) => {
                          event.stopPropagation();
                          void copyPrefix(key);
                        }}
                      >
                        {formatKeyPrefix(key.prefix)}
                        {copiedPrefixId === key.id ? <Check className="text-success" /> : <Copy className="text-muted-foreground" />}
                      </Button>
                    </TableCell>
                    <TableCell>
                      <Badge variant={key.readOnly ? "default" : key.revokedAt ? "outline" : "secondary"}>
                        {key.readOnly ? "root" : key.revokedAt ? "revoked" : "active"}
                      </Badge>
                    </TableCell>
                    <TableCell className="hidden text-right tabular-nums md:table-cell">
                      {requests ? requests.toLocaleString() : "—"}
                    </TableCell>
                    <TableCell className="text-right tabular-nums" title={tokens ? exactTokens(tokens) : undefined}>
                      {tokens ? formatTokens(tokens) : "—"}
                    </TableCell>
                    <TableCell className="hidden text-xs text-muted-foreground md:table-cell">
                      {key.readOnly ? "—" : formatTimestamp(key.createdAt)}
                    </TableCell>
                    <TableCell className="text-right">
                      {key.readOnly ? null : (
                        <div className="flex items-center justify-end gap-1">
                          <Button
                            size="xs"
                            variant="ghost"
                            className="text-muted-foreground hover:text-foreground"
                            disabled={isMutating}
                            aria-label={`Rename key ${key.name}`}
                            title="Rename"
                            onClick={(event) => {
                              event.stopPropagation();
                              setNewName(key.name);
                              setRenaming(key);
                            }}
                          >
                            <Pencil size={14} />
                          </Button>
                          <Button
                            size="xs"
                            variant="ghost"
                            className="text-muted-foreground hover:text-destructive"
                            disabled={isMutating}
                            aria-label={`Delete key ${key.name}`}
                            title="Delete"
                            onClick={(event) => {
                              event.stopPropagation();
                              setDeleting(key);
                            }}
                          >
                            <Trash2 size={14} />
                          </Button>
                        </div>
                      )}
                    </TableCell>
                  </TableRow>,
                  isOpen ? (
                    <TableRow key={`${key.id}-usage`} id={usageRegionId}>
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

      <Dialog open={deleting !== null} onOpenChange={(open) => !open && setDeleting(null)}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Delete this API key?</DialogTitle>
            <DialogDescription>
              {deleting ? (
                <>
                  <span className="font-medium">{deleting.name}</span> stops working immediately and its usage
                  history is removed. This cannot be undone.
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
                if (deleting) onDelete(deleting.id);
                setDeleting(null);
              }}
            >
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={renaming !== null} onOpenChange={(open) => !open && setRenaming(null)}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Rename API key</DialogTitle>
            <DialogDescription>The key itself does not change, only its label.</DialogDescription>
          </DialogHeader>
          <div className="space-y-1">
            <Label htmlFor="key-name">Name</Label>
            <Input
              id="key-name"
              value={newName}
              maxLength={80}
              disabled={isMutating}
              onChange={(event) => setNewName(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && newName.trim() && renaming) {
                  onRename(renaming.id, newName.trim());
                  setRenaming(null);
                }
              }}
            />
          </div>
          <DialogFooter>
            <Button variant="outline" disabled={isMutating} onClick={() => setRenaming(null)}>
              Cancel
            </Button>
            <Button
              disabled={isMutating || !newName.trim() || newName.trim() === renaming?.name}
              onClick={() => {
                if (renaming) onRename(renaming.id, newName.trim());
                setRenaming(null);
              }}
            >
              Save
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}
