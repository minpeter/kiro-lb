import { useEffect, useState, type ReactNode } from "react";
import { Eye, ScrollText } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { EmptyState } from "@/components/empty-state";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { dashboardApi, DashboardApiError } from "../api";
import { formatLatency, formatRelativeTime, formatTimestamp } from "../format";
import type { RequestLogDetail, RequestLogOrder, RequestLogPage } from "../types";
import { PaginationControls } from "./pagination-controls";
import { TableSkeleton } from "./skeletons";

export type RequestLogTableProps = {
  page: RequestLogPage;
  isLoading: boolean;
  model: string;
  order: RequestLogOrder;
  onLimitChange: (limit: number) => void;
  onOffsetChange: (offset: number) => void;
  onModelChange: (model: string) => void;
  onOrderChange: (order: RequestLogOrder) => void;
};

const ALL_MODELS = "__all__";

export function RequestLogTable({
  page,
  isLoading,
  model,
  order,
  onLimitChange,
  onOffsetChange,
  onModelChange,
  onOrderChange,
}: RequestLogTableProps) {
  const isEmpty = !isLoading && page.total === 0;
  const [detail, setDetail] = useState<RequestLogDetail | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [loadingDetail, setLoadingDetail] = useState<number | null>(null);
  // One shared "now" for every row, ticked coarsely so a long-lived tab does
  // not freeze at "just now"; the lazy initializer keeps the impure Date.now()
  // out of the render body.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, []);

  const openDetail = async (id: number) => {
    setLoadingDetail(id);
    setDetailError(null);
    try {
      setDetail(await dashboardApi.requestLogDetail(id));
    } catch (error) {
      setDetailError(error instanceof DashboardApiError ? error.message : "Could not load the request");
    } finally {
      setLoadingDetail(null);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Recent proxy requests</CardTitle>
        <CardDescription>
          Open a row to see where it came from and what was sent. Prompt text appears only if capture is on
          in Settings; it is encrypted at rest.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex flex-wrap items-center gap-2">
          <Select value={model || ALL_MODELS} onValueChange={(value) => onModelChange(value === ALL_MODELS ? "" : value)}>
            <SelectTrigger className="w-56" aria-label="Filter by model">
              <SelectValue placeholder="All models" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL_MODELS}>All models</SelectItem>
              {(page.models ?? []).map((name) => (
                <SelectItem key={name} value={name}>
                  {name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Select value={order} onValueChange={(value) => onOrderChange(value as RequestLogOrder)}>
            <SelectTrigger className="w-44" aria-label="Sort order">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="newest">Newest first</SelectItem>
              <SelectItem value="oldest">Oldest first</SelectItem>
            </SelectContent>
          </Select>
        </div>

        {detailError && (
          <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {detailError}
          </div>
        )}

        {isLoading ? (
          <TableSkeleton rows={Math.min(page.limit, 5)} columns={6} />
        ) : isEmpty ? (
          <EmptyState icon={ScrollText} title="No requests recorded yet" description="Proxy traffic will appear here." />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Time</TableHead>
                <TableHead>Route</TableHead>
                <TableHead>Model</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="hidden text-right md:table-cell">Latency</TableHead>
                <TableHead className="w-10" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {page.logs.map((log, index) => (
                <TableRow key={log.id ?? `${log.created_at}-${page.offset + index}`}>
                  <TableCell title={formatTimestamp(log.created_at)}>
                    {formatRelativeTime(now, log.created_at)}
                  </TableCell>
                  <TableCell className="max-w-[10rem] truncate font-mono text-xs md:max-w-none">{log.route}</TableCell>
                  <TableCell>
                    {log.model ?? "—"}
                    {log.credits ? (
                      <span className="ml-2 text-xs text-muted-foreground">{log.credits}x</span>
                    ) : null}
                  </TableCell>
                  <TableCell>
                    {/* Status is the point of the table, so both states must read at a
                        glance: a tinted outline for success against the loud destructive pill. */}
                    <Badge
                      variant={log.status_code < 400 ? "outline" : "destructive"}
                      className={log.status_code < 400 ? "border-success/40 text-success" : undefined}
                    >
                      {log.status_code}
                    </Badge>
                  </TableCell>
                  <TableCell className="hidden text-right tabular-nums md:table-cell">
                    {formatLatency(log.latency_ms)}
                  </TableCell>
                  <TableCell className="text-right">
                    {log.id ? (
                      <Button
                        variant="ghost"
                        size="icon"
                        className="size-7"
                        aria-label="Show request details"
                        disabled={loadingDetail !== null}
                        onClick={() => void openDetail(log.id as number)}
                      >
                        <Eye size={14} aria-hidden />
                      </Button>
                    ) : null}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
      {isEmpty ? null : (
        <CardFooter className="border-t">
          <PaginationControls
            total={page.total}
            limit={page.limit}
            offset={page.offset}
            hasMore={page.hasMore}
            onLimitChange={onLimitChange}
            onOffsetChange={onOffsetChange}
          />
        </CardFooter>
      )}
      <RequestDetailDialog detail={detail} onClose={() => setDetail(null)} />
    </Card>
  );
}

function Field({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="space-y-1">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="text-sm break-all">{value ?? "—"}</p>
    </div>
  );
}

function RequestDetailDialog({ detail, onClose }: { detail: RequestLogDetail | null; onClose: () => void }) {
  return (
    <Dialog open={detail !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-h-[85vh] overflow-auto sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle>Request detail</DialogTitle>
          <DialogDescription>
            {detail ? `${detail.route} · ${formatTimestamp(detail.createdAt)}` : ""}
          </DialogDescription>
        </DialogHeader>
        {detail && (
          <div className="space-y-4">
            <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
              <Field label="Model" value={detail.model} />
              <Field label="Status" value={detail.statusCode} />
              <Field label="Latency" value={formatLatency(detail.latencyMs)} />
              <Field label="Client" value={detail.clientIp} />
              <Field label="User agent" value={detail.userAgent} />
              <Field
                label="Tokens in / out"
                value={
                  detail.inputTokens !== null || detail.outputTokens !== null
                    ? `${(detail.inputTokens ?? 0).toLocaleString()} / ${(detail.outputTokens ?? 0).toLocaleString()}`
                    : "—"
                }
              />
            </div>

            <p className="text-xs text-muted-foreground">
              The gateway records metadata only; request and response text is never stored.
            </p>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
