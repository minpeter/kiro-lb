import { useEffect, useRef, useState } from "react";
import { Check, ChevronDown, ChevronRight, Copy, KeyRound, Pencil, ShieldCheck, Trash2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
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
import { keyLimitReached, limitFieldValue, limitReached, parseLimit } from "../key-limits";
import type { ApiKey, ApiKeyPatch, KeyModelUsage, KeyUsage } from "../types";
import { KeyLimitFields } from "./key-limit-fields";
import { KeyModelFields } from "./key-model-fields";
import { TableSkeleton } from "./skeletons";
import { usePreferences } from "../preferences";

export type ApiKeysPanelProps = {
  apiKeys: ApiKey[];
  keyUsage: KeyUsage;
  isLoading: boolean;
  isMutating: boolean;
  onCreate: () => void;
  onDelete: (id: string) => void;
  onUpdate: (id: string, patch: ApiKeyPatch) => void;
};

function sameModels(a: string[] | null, b: string[] | null) {
  if (a === null || b === null) return a === b;
  return a.length === b.length && a.every((m) => b.includes(m));
}

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
  const { t } = usePreferences();
  if (rows.length === 0) {
    return <p className="px-4 py-3 text-xs text-muted-foreground">{t("keys.noTraffic")}</p>;
  }
  return (
    <div className="bg-muted/40 px-4 py-3">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>{t("keys.col.model")}</TableHead>
            <TableHead className="text-right">{t("keys.col.requests")}</TableHead>
            <TableHead className="text-right">{t("keys.col.input")}</TableHead>
            <TableHead className="text-right">{t("keys.col.output")}</TableHead>
            <TableHead className="text-right">{t("keys.col.total")}</TableHead>
            <TableHead>{t("keys.col.lastUsed")}</TableHead>
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
  onUpdate,
}: ApiKeysPanelProps) {
  const { t } = usePreferences();
  const [expanded, setExpanded] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<ApiKey | null>(null);
  const [editing, setEditing] = useState<ApiKey | null>(null);
  const [newName, setNewName] = useState("");
  const [requestLimit, setRequestLimit] = useState("");
  const [tokenLimit, setTokenLimit] = useState("");
  const [allowedModels, setAllowedModels] = useState<string[] | null>(null);

  const openEditor = (key: ApiKey) => {
    setNewName(key.name);
    setRequestLimit(limitFieldValue(key.requestLimit));
    setTokenLimit(limitFieldValue(key.tokenLimit));
    setAllowedModels(key.allowedModels ?? null);
    setEditing(key);
  };

  const editedRequests = parseLimit(requestLimit);
  const editedTokens = parseLimit(tokenLimit);
  const editPatch: ApiKeyPatch | null =
    editing && editedRequests.ok && editedTokens.ok && newName.trim() && !(allowedModels !== null && allowedModels.length === 0)
      ? {
          ...(newName.trim() !== editing.name ? { name: newName.trim() } : {}),
          ...(editedRequests.value !== (editing.requestLimit ?? null) ? { requestLimit: editedRequests.value } : {}),
          ...(editedTokens.value !== (editing.tokenLimit ?? null) ? { tokenLimit: editedTokens.value } : {}),
          ...(!sameModels(allowedModels, editing.allowedModels ?? null) ? { allowedModels } : {}),
        }
      : null;
  const canSave = editPatch !== null && Object.keys(editPatch).length > 0;
  const saveEdit = () => {
    if (editing && editPatch && canSave) onUpdate(editing.id, editPatch);
    setEditing(null);
  };
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
            <CardTitle>{t("keys.title")}</CardTitle>
          </div>
          <Button size="sm" disabled={isMutating} onClick={onCreate}>
            <KeyRound />
            {t("keys.createKey")}
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <TableSkeleton rows={3} columns={6} />
        ) : apiKeys.length === 0 ? (
          <EmptyState icon={KeyRound} title={t("keys.emptyTitle")} description={t("keys.emptyDescription")} />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-8" />
                <TableHead>{t("keys.col.name")}</TableHead>
                <TableHead>{t("keys.col.prefix")}</TableHead>
                <TableHead>{t("keys.col.status")}</TableHead>
                <TableHead className="hidden text-right md:table-cell">{t("keys.col.requests")}</TableHead>
                <TableHead className="text-right">{t("keys.col.tokens")}</TableHead>
                <TableHead className="hidden md:table-cell">{t("keys.col.created")}</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {apiKeys.map((key) => {
                const rows = keyUsage[key.id] ?? [];
                const { tokens, requests } = totals(rows);
                const atLimit = keyLimitReached({ tokens, requests }, key);
                const isOpen = expanded === key.id;
                const usageRegionId = `api-key-usage-${key.id}`;
                const toggleExpanded = () => setExpanded(isOpen ? null : key.id);
                return [
                  <TableRow
                    key={key.id}
                    className="cursor-pointer focus-visible:bg-accent/50 focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:ring-inset focus-visible:outline-none"
                    tabIndex={0}
                    aria-label={t("keys.usageAria", { name: key.name })}
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
                        {key.readOnly && <Badge variant="secondary">{t("keys.environment")}</Badge>}
                      </span>
                    </TableCell>
                    <TableCell>
                      <Button
                        variant="ghost"
                        size="xs"
                        className="h-auto gap-1 px-1 font-mono text-xs font-normal"
                        aria-label={t("keys.copyPrefixAria", { name: key.name })}
                        title={t("keys.copyPrefix")}
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
                      <span className="flex flex-wrap items-center gap-1">
                        <Badge variant={key.readOnly ? "default" : key.revokedAt ? "outline" : "secondary"}>
                          {t(key.readOnly ? "keys.status.root" : key.revokedAt ? "keys.status.revoked" : "keys.status.active")}
                        </Badge>
                        {atLimit && <Badge variant="destructive">{t("keys.status.limit")}</Badge>}
                        {key.allowedModels && (
                          <Badge variant="outline" title={key.allowedModels.join(", ")}>
                            {t("keys.models.count", { n: key.allowedModels.length })}
                          </Badge>
                        )}
                      </span>
                    </TableCell>
                    <TableCell
                      className={`hidden text-right tabular-nums md:table-cell ${limitReached(requests, key.requestLimit) ? "text-destructive" : ""}`}
                    >
                      {requests ? requests.toLocaleString() : "—"}
                      {key.requestLimit != null && (
                        <span className="text-muted-foreground"> / {key.requestLimit.toLocaleString()}</span>
                      )}
                    </TableCell>
                    <TableCell
                      className={`text-right tabular-nums ${limitReached(tokens, key.tokenLimit) ? "text-destructive" : ""}`}
                      title={tokens ? exactTokens(tokens) : undefined}
                    >
                      {tokens ? formatTokens(tokens) : "—"}
                      {key.tokenLimit != null && (
                        <span className="text-muted-foreground" title={exactTokens(key.tokenLimit)}>
                          {" "}
                          / {formatTokens(key.tokenLimit)}
                        </span>
                      )}
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
                            aria-label={t("keys.editAria", { name: key.name })}
                            title={t("keys.edit")}
                            onClick={(event) => {
                              event.stopPropagation();
                              openEditor(key);
                            }}
                          >
                            <Pencil size={14} />
                          </Button>
                          <Button
                            size="xs"
                            variant="ghost"
                            className="text-muted-foreground hover:text-destructive"
                            disabled={isMutating}
                            aria-label={t("keys.deleteAria", { name: key.name })}
                            title={t("keys.delete")}
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
            <DialogTitle>{t("keys.deleteTitle")}</DialogTitle>
            <DialogDescription>
              {deleting ? (
                <>
                  <span className="font-medium">{deleting.name}</span> {t("keys.deleteDescription")}
                </>
              ) : null}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" disabled={isMutating} onClick={() => setDeleting(null)}>
              {t("keys.cancel")}
            </Button>
            <Button
              variant="destructive"
              disabled={isMutating}
              onClick={() => {
                if (deleting) onDelete(deleting.id);
                setDeleting(null);
              }}
            >
              {t("keys.delete")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={editing !== null} onOpenChange={(open) => !open && setEditing(null)}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{t("keys.editTitle")}</DialogTitle>
            <DialogDescription>{t("keys.editDescription")}</DialogDescription>
          </DialogHeader>
          <div className="space-y-1">
            <Label htmlFor="key-name">{t("keys.col.name")}</Label>
            <Input
              id="key-name"
              value={newName}
              maxLength={80}
              disabled={isMutating}
              onChange={(event) => setNewName(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && canSave) saveEdit();
              }}
            />
          </div>
          <KeyLimitFields
            idPrefix="edit-key"
            requestLimit={requestLimit}
            tokenLimit={tokenLimit}
            disabled={isMutating}
            onRequestLimitChange={setRequestLimit}
            onTokenLimitChange={setTokenLimit}
          />
          <KeyModelFields idPrefix="edit-key" value={allowedModels} disabled={isMutating} onChange={setAllowedModels} />
          <DialogFooter>
            <Button variant="outline" disabled={isMutating} onClick={() => setEditing(null)}>
              {t("keys.cancel")}
            </Button>
            <Button disabled={isMutating || !canSave} onClick={saveEdit}>
              {t("keys.save")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}
